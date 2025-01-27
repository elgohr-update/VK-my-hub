from kubernetes.client import models as k8s
from . import databricks
from airflow.exceptions import AirflowException
from airflow.models import BaseOperator, SkipMixin
from airflow.models.dag import DagContext
from airflow.utils.task_group import TaskGroup
from airflow.utils.decorators import apply_defaults
from airflow.utils.file import TemporaryDirectory
from airflow.utils.operator_helpers import context_to_airflow_vars
from airflow.utils.state import State
import papermill as pm
import os
import smtplib
import json

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import textwrap

from airflow.providers.cncf.kubernetes.operators.kubernetes_pod import (
    KubernetesPodOperator,
)



# import config file
import configparser
config = configparser.ConfigParser()
config.read('/defaults.cfg')


def common_write_mail(self, outputFileName):

    # skip mailing if it is not the last retry
    if self.ti.max_tries >= self.ti.try_number:
        return

    try:
        print("Render HTML")
        os.system(
            "jupyter nbconvert --to HTML --no-input {0}".format(outputFileName))
    except:
        print("Render failed")
        pass

    try:
        print("Write Mails")
        # get all mail parameters
        server = smtplib.SMTP(config["Airflow"]["smtp"])
        serverMail = config["Airflow"]["fromMail"]
        toMail = config["Airflow"]["toMail"]
        toMail = [m.replace(" ", "") for m in toMail.split(",")]

        # add special users from aiflow call
        if hasattr(self, 'email') and self.email != None:
            toMail.extend([m.replace(" ", "")
                           for m in self.email.split(",")])

        # start to send the mails
        m = toMail[0]

        msg = MIMEMultipart()
        msg['Subject'] = 'Notebook Exec Error'
        msg['From'] = serverMail
        msg['To'] = m
        msg['Cc'] = ", ".join([tm for tm in toMail if tm != m])

        text = "Hi!\nThe PapermillOperator of {} raised an exception".format(
            self.inputFile)
        part1 = MIMEText(text, 'plain')
        msg.attach(part1)

        try:
            with open(os.path.splitext(outputFileName)[0]+'.html', 'r') as file:
                html = file.read()
            part2 = MIMEText(html, 'html')
            msg.attach(part2)
        except:
            pass

        server.sendmail(serverMail, toMail, msg.as_string())
    except:
        print("Error writing mails")


def common_execute(self, context):
    self.runDate = context['execution_date']
    self.dagName = context['dag'].dag_id
    self.dagfolder = context['dag'].folder
    self.ti = context['ti']
    self.dagrun = self.ti.get_dagrun()
    self.parameters["conf"] = json.dumps(self.dagrun.conf)

    outputFileName = os.path.join(
        "/home/admin/workflow/output", self.dagName, self.runDate.strftime("%Y-%m-%d_%H_%M"), self.outputFile)
    workingDir = os.path.dirname(outputFileName)

    self.parameters["workflow"] = json.dumps({
        "dagBase": "/home/admin/workflow/dags",
        "dagFolder": self.dagfolder,
        "workingDir": workingDir,
        "fileStore": "/home/admin/workflow/FileStore"
    })

    return_value = self.execute_callable()
    self.log.info("Done. Returned value was: %s", return_value)
    return return_value


def common_execute_callable(self, prepare_only=False):
    outputFileName = os.path.join(
        "/home/admin/workflow/output", self.dagName, self.runDate.strftime("%Y-%m-%d_%H_%M"), self.outputFile)
    workingDir = os.path.dirname(outputFileName)

    res = {}

    if not os.path.isdir(workingDir):
        os.makedirs(workingDir)

    try:
        res = pm.execute_notebook(
            os.path.join(self.dagfolder, self.inputFile),
            os.path.join("/home/admin/workflow/output",
                         self.dagName,
                         self.runDate.strftime("%Y-%m-%d_%H_%M"), self.outputFile),
            cwd=workingDir,
            parameters=self.parameters,
            prepare_only=prepare_only
        )
    except Exception as ex:

        common_write_mail(self, outputFileName)

        raise ex

    return res


class PapermillOperator(BaseOperator):
    """

    Executes a Jupyter Notebook with papermill locally.

    Attributes
    ----------
    inputFile : str
        the input Jupyter Notebook
    outputFile : str
        the output Jupyter Notebook
    parameters : dict
        additional parameters for the run
    """
    template_fields = ('templates_dict',)
    template_ext = tuple()
    ui_color = '#ffcca9'

    # since we won't mutate the arguments, we should just do the shallow copy
    # there are some cases we can't deepcopy the objects(e.g protobuf).
    shallow_copy_attrs = ('python_callable', 'op_kwargs',)

    @apply_defaults
    def __init__(
            self,
            inputFile,
            outputFile,
            parameters=None,
            op_args=None,
            op_kwargs=None,
            # provide_context=False,
            templates_dict=None,
            templates_exts=None,
            *args, **kwargs):
        super(PapermillOperator, self).__init__(*args, **kwargs)
        self.op_args = op_args or []
        self.op_kwargs = op_kwargs or {}
        #self.provide_context = provide_context
        self.templates_dict = templates_dict
        if templates_exts:
            self.template_ext = templates_exts
        self.inputFile = inputFile
        self.outputFile = outputFile

        self.parameters = {
            "params": json.dumps(parameters)
        }

    def execute(self, context):
        return common_execute(self, context)

    def execute_callable(self):
        return common_execute_callable(self, prepare_only=False)



class PapermillOperatorK8s(KubernetesPodOperator):
    """

    Executes a Jupyter Notebook with papermill in a kubernetes worker.

    Attributes
    ----------
    inputFile : str
        the input Jupyter Notebook
    outputFile : str
        the output Jupyter Notebook
    parameters : dict
        additional parameters for the run
    image : str
        the container image you want to use
    namespace: str
        the namespace you want to run in
    name : str
        name of the worker container
    """

    template_ext = tuple()
    ui_color = '#a9b7ff'

    def __init__(
            self,
            inputFile,
            outputFile,
            parameters={},
            op_args=None,
            op_kwargs=None,
            *args, **kwargs):

        if "image" not in kwargs:
            kwargs["image"] = config["Airflow"]["image"]
        if "name" not in kwargs:
            kwargs["name"] = "airflow-" + kwargs["task_id"]
        if "namespace" not in kwargs:
            kwargs["namespace"] = config["Airflow"]["namespace"]
        kwargs["do_xcom_push"] = False

        kwargs["volume_mounts"] = [
            k8s.V1VolumeMount(mount_path='/home/admin/workflow/output', name='output-data', sub_path=None, read_only=False)
        ]
        kwargs["volumes"] = [k8s.V1Volume(
            name='output-data',
            persistent_volume_claim=k8s.V1PersistentVolumeClaimVolumeSource(claim_name='output-data'),
        )]

        kwargs["cmds"] = ["bash", "-cx"]
        kwargs["arguments"] = []
        
        super(PapermillOperatorK8s, self).__init__(*args, **kwargs)

        self.inputFile = inputFile
        self.outputFile = outputFile

        self.parameters = {
            "params": json.dumps(parameters)
        }

    def execute_callable(self):
        return common_execute_callable(self, prepare_only=True)

    def execute(self, context):
        return_value = common_execute(self, context)

        outputFileName = os.path.join(
            "/home/admin/workflow/output", self.dagName, self.runDate.strftime("%Y-%m-%d_%H_%M"), self.outputFile)
        workingDir = os.path.dirname(outputFileName)

        self.cmds = ["bash", "-cx", f"cd {workingDir} && papermill {outputFileName}" ]

        try:
            return_value = KubernetesPodOperator.execute(self, context)
        except Exception as ex:
            common_write_mail(self, outputFileName)

            raise ex

        return return_value



class LibraryOperator(BaseOperator):
    """

    Create libraries for local use and databricks

    Attributes
    ----------
    libFolder : str
        the folder name of the python library
    outputFile : str
        the folder used for the output
    version : str
        version string like 1.0.0
    to_databricks: bool
        copy the library also to databricks
    """
    template_fields = ('templates_dict',)
    template_ext = tuple()
    ui_color = '#f5e569'

    # since we won't mutate the arguments, we should just do the shallow copy
    # there are some cases we can't deepcopy the objects(e.g protobuf).
    shallow_copy_attrs = ('python_callable', 'op_kwargs',)

    @apply_defaults
    def __init__(
            self,
            libFolder,
            outputFolder="",
            version="1.0.0",
            to_databricks=True,
            op_args=None,
            op_kwargs=None,
            templates_dict=None,
            templates_exts=None,
            *args, **kwargs):
        super(LibraryOperator, self).__init__(*args, **kwargs)
        self.op_args = op_args or []
        self.op_kwargs = op_kwargs or {}
        #self.provide_context = provide_context
        self.templates_dict = templates_dict
        if templates_exts:
            self.template_ext = templates_exts

        self.libFolder = libFolder
        self.outputFolder = outputFolder
        self.libName = os.path.basename(self.libFolder)
        self.version = version
        self.to_databricks = to_databricks

        self.whlfile = "{}-{}-py2.py3-none-any.whl".format(
            self.libName, self.version)

    def execute(self, context):
        self.dagfolder = context['dag'].folder
        self.fullLibFolder = os.path.join(self.dagfolder, self.libFolder)
        self.dagName = context['dag'].dag_id

        # create the output of the local library copy
        self.runDate = context['execution_date']
        outputFolder = os.path.join(
            "/home/admin/workflow/output", self.dagName, self.runDate.strftime("%Y-%m-%d_%H_%M"), self.outputFolder, self.libName)

        if not os.path.isdir(outputFolder):
            os.makedirs(outputFolder)

        # copy the library content
        from distutils.dir_util import copy_tree
        copy_tree(self.fullLibFolder, outputFolder)

        # create a temporary directory
        import tempfile
        tempdir = tempfile.TemporaryDirectory()

        # create an basic library in the temp directory
        from pypc.create import Package
        p = Package(self.libName, path=tempdir.name)
        p.new(pkgname=self.libName)

        # copy the library content to the temp folder
        copy_tree(self.fullLibFolder, os.path.join(tempdir.name, self.libName))

        # build the package
        import subprocess
        res = subprocess.call(
            ['python', 'setup.py', 'bdist_wheel'], cwd=tempdir.name)
        if res != 0:
            raise Exception("library build process failed")

        # copy the library package to the local FileStore
        if not os.path.isdir("/home/admin/workflow/FileStore/libs"):
            os.makedirs("/home/admin/workflow/FileStore/libs")
        createdWhlFile = os.listdir(os.path.join(tempdir.name, "dist"))[0]
        from shutil import copyfile
        copyfile(os.path.join(tempdir.name, "dist", createdWhlFile), os.path.join(
            "/home/admin/workflow/FileStore/libs", self.whlfile))

        # sync library file to databricks
        if self.to_databricks:
            dbr = databricks.Databricks()
            dbr.upload_file(os.path.join("libs", self.whlfile))

        # append to libs log
        with open(
                os.path.join(
                    "/home/admin/workflow/output",
                    self.dagName,
                    self.runDate.strftime("%Y-%m-%d_%H_%M"),
                    self.outputFolder,
                    "LibraryOperator"),
                "a") as f:
            f.write("{} => {}".format(self.libName, self.whlfile))

        tempdir.cleanup()

        return True


class DatabricksOperator(BaseOperator):
    """

    Executes a Jupyter Notebook on Databricks


    Attributes
    ----------
    inputFile : str
        the input Jupyter Notebook
    outputFile : str
        the output Jupyter Notebook
    parameters : dict
        additional parameters for the run
    libraries : array
        libraries added to databricks
    new_cluster : dict
        parameters for the cluster to create
    existing_cluster_id : str
        name of an existing cluster
    terminate_cluster : bool
        terminate cluster after finishing the job
    """
    template_fields = ('templates_dict',)
    template_ext = tuple()
    ui_color = '#a9ceff'

    # since we won't mutate the arguments, we should just do the shallow copy
    # there are some cases we can't deepcopy the objects(e.g protobuf).
    shallow_copy_attrs = ('python_callable', 'op_kwargs',)

    @apply_defaults
    def __init__(
            self,
            inputFile,
            outputFile,
            libraries=None,
            parameters=None,
            dry_run=False,
            op_args=None,
            op_kwargs=None,
            new_cluster=None,
            existing_cluster_id=None,
            terminate_cluster=True,
            # provide_context=False,
            templates_dict=None,
            templates_exts=None,
            *args, **kwargs):
        super(DatabricksOperator, self).__init__(*args, **kwargs)
        self.op_args = op_args or []
        self.op_kwargs = op_kwargs or {}
        #self.provide_context = provide_context
        self.templates_dict = templates_dict
        if templates_exts:
            self.template_ext = templates_exts
        self.inputFile = inputFile
        self.outputFile = outputFile
        self.libraries = libraries

        self.parameters = {
            "params": json.dumps(parameters)
        }
        self.new_cluster = new_cluster
        self.existing_cluster_id = existing_cluster_id
        self.terminate_cluster = terminate_cluster if existing_cluster_id else False
        self.dry_run = dry_run

    def add_library(self, lib: LibraryOperator):
        """
        add library to the databricks job
        """
        if not self.libraries:
            self.libraries = []

        user = config["Databricks"]["USER"]

        self.libraries.append(
            {
                "whl":  os.path.join("dbfs:/FileStore/shared_uploads", user, "libs",  lib.whlfile)
            }
        )

        print(self.libraries)

        lib >> self

    def execute(self, context):
        self.runDate = context['execution_date']
        self.dagName = context['dag'].dag_id
        self.dagfolder = context['dag'].folder
        self.ti = context['ti']
        self.dagrun = self.ti.get_dagrun()
        self.parameters["conf"] = json.dumps(self.dagrun.conf)

        outputFileName = os.path.join(
            "/home/admin/workflow/output", self.dagName, self.runDate.strftime("%Y-%m-%d_%H_%M"), self.outputFile)
        workingDir = os.path.dirname(outputFileName)

        dbr = databricks.Databricks()

        self.parameters["workflow"] = json.dumps({
            "dagBase": dbr._get_fullpath(""),
            "dagFolder": dbr._get_fullpath(os.path.join(self.dagfolder)[26:]),
            "workingDir": workingDir,
            "fileStore": "/dbfs/FileStore/shared_uploads/viktor.krueckl@osram-os.com/"
        })

        return_value = self.execute_callable()
        self.log.info("Done. Returned value was: %s", return_value)
        return return_value

    def execute_callable(self):
        outputFileName = os.path.join(
            "/home/admin/workflow/output", self.dagName, self.runDate.strftime("%Y-%m-%d_%H_%M"), self.outputFile)
        workingDir = os.path.dirname(outputFileName)
        fileName = outputFileName[len(workingDir)+1:]

        res = {}

        if not os.path.isdir(workingDir):
            os.makedirs(workingDir)

        try:

            dbr = databricks.Databricks()

            targetFile = os.path.join(self.dagfolder, self.inputFile)[26:]

            dbr.mkdirs(os.path.dirname(targetFile))

            dbr.import_ipynb(
                os.path.join(self.dagfolder, self.inputFile),
                targetFile)

            job = dbr.assure_job(
                targetFile,
                targetFile,
                self.new_cluster,
                self.existing_cluster_id,
                self.libraries
            )

            if self.dry_run == False:
                print("job_id: {}".format(job["job_id"]))
                run = dbr.run_job(job["job_id"], self.parameters)

                print("run_id: {}".format(run["run_id"]))
                run_res = dbr.await_run(run["run_id"])

                dbr.run_export(run["run_id"], outputFileName)

                if run_res["state"]["result_state"] != "SUCCESS":
                    raise Exception("Databricks run failed")

                if self.terminate_cluster:
                    dbr.terminate_cluster(self.existing_cluster_id)

        except Exception as ex:

            # skip mailing if it is not the last retry
            if self.ti.max_tries >= self.ti.try_number:
                raise ex

            try:
                print("Write Mails")
                # get all mail parameters
                server = smtplib.SMTP(config["Airflow"]["smtp"])
                serverMail = config["Airflow"]["fromMail"]
                toMail = config["Airflow"]["toMail"]
                toMail = [m.replace(" ", "") for m in toMail.split(",")]

                # add special users from aiflow call
                if hasattr(self, 'email') and self.email != None:
                    toMail.extend([m.replace(" ", "")
                                   for m in self.email.split(",")])

                # start to send the mails
                m = toMail[0]

                msg = MIMEMultipart()
                msg['Subject'] = 'Notebook Exec Error'
                msg['From'] = serverMail
                msg['To'] = m
                msg['Cc'] = ", ".join([tm for tm in toMail if tm != m])

                text = "Hi!\nThe DatabricksOperator of {} raised an exception".format(
                    self.inputFile)
                part1 = MIMEText(text, 'plain', 'utf-8')
                msg.attach(part1)

                print(workingDir)
                for fname in [el for el in os.listdir(workingDir) if el.lower().startswith(fileName.lower())]:
                    try:
                        print(fname)
                        print(os.path.join(workingDir, fname))
                        with open(os.path.join(workingDir, fname), 'r') as file:
                            html = file.read().encode('utf-8')
                        part2 = MIMEText(html, 'html', 'utf-8')
                        msg.attach(part2)
                    except:
                        pass

                server.sendmail(serverMail, toMail, msg.as_string())
            except:
                print("Error writing mails")

            raise ex

        return res


class UploadToDatabricks(BaseOperator):
    """

    Copy a file or more from the local FileStore to the Databricks FileStore

    Attributes
    ----------
    inputFile : str, list
        the file to copy to the Databricks FileStore
    """
    template_fields = ('templates_dict',)
    template_ext = tuple()
    ui_color = '#a9ffa9'

    # since we won't mutate the arguments, we should just do the shallow copy
    # there are some cases we can't deepcopy the objects(e.g protobuf).
    shallow_copy_attrs = ('python_callable', 'op_kwargs',)

    @apply_defaults
    def __init__(
            self,
            inputFile,
            op_args=None,
            op_kwargs=None,
            templates_dict=None,
            templates_exts=None,
            *args, **kwargs):
        super(UploadToDatabricks, self).__init__(*args, **kwargs)
        self.op_args = op_args or []
        self.op_kwargs = op_kwargs or {}
        self.templates_dict = templates_dict
        if templates_exts:
            self.template_ext = templates_exts

        self.inputFile = inputFile

    def execute(self, context):
        return_value = self.execute_callable()
        self.log.info("Done. Returned value was: %s", return_value)
        return return_value

    def execute_callable(self):

        dbr = databricks.Databricks()

        if isinstance(self.inputFile, list):
            for el in self.inputFile:
                dbr.upload_file(el)
        else:
            dbr.upload_file(self.inputFile)

        return {}


class DownloadFromDatabricks(BaseOperator):
    """

    Copy a file or more from the Databricks FileStore to the local FileStore

    Attributes
    ----------
    inputFile : str, list
        the file to collect from the Databricks FileStore
    """
    template_fields = ('templates_dict',)
    template_ext = tuple()
    ui_color = '#89cc89'

    # since we won't mutate the arguments, we should just do the shallow copy
    # there are some cases we can't deepcopy the objects(e.g protobuf).
    shallow_copy_attrs = ('python_callable', 'op_kwargs',)

    @apply_defaults
    def __init__(
            self,
            inputFile,
            op_args=None,
            op_kwargs=None,
            templates_dict=None,
            templates_exts=None,
            *args, **kwargs):
        super(DownloadFromDatabricks, self).__init__(*args, **kwargs)
        self.op_args = op_args or []
        self.op_kwargs = op_kwargs or {}
        self.templates_dict = templates_dict
        if templates_exts:
            self.template_ext = templates_exts

        self.inputFile = inputFile

    def execute(self, context):
        return_value = self.execute_callable()
        self.log.info("Done. Returned value was: %s", return_value)
        return return_value

    def execute_callable(self):

        dbr = databricks.Databricks()

        if isinstance(self.inputFile, list):
            for el in self.inputFile:
                dbr.download_file(el)
        else:
            dbr.download_file(self.inputFile)

        return {}


class RetryTaskGroup(TaskGroup):
    """
    A collection of tasks based on the airflow.utils.task_group.TaskGroup.
    When set_downstream() or set_upstream() are called on the TaskGroup, it is
    applied across all tasks within the group if necessary.
    When one of the Tasks needs an retry. All tasks make a retry.
    :param group_id: a unique, meaningful id for the TaskGroup. group_id must
        not conflict with group_id of TaskGroup or task_id of tasks in the DAG.
        Root TaskGroup has group_id set to None.
    :type group_id: str
    :param retries: the number of retries that should be performed before
        failing the task. Default set to 3.
    :type retries: int
    :param prefix_group_id: If set to True, child task_id and group_id will be
        prefixed with this TaskGroup's group_id. If set to False, child task_id
        and group_id are not prefixed.
        Default is True.
    :type prefix_group_id: bool
    :param parent_group: The parent TaskGroup of this TaskGroup. parent_group
        is set to None for the root TaskGroup.
    :type parent_group: TaskGroup
    :param dag: The DAG that this TaskGroup belongs to.
    :type dag: airflow.models.DAG
    :param tooltip: The tooltip of the TaskGroup node when displayed in the UI
    :type tooltip: str
    :param ui_color: The fill color of the TaskGroup node when displayed in
        the UI
    :type ui_color: str
    :param ui_fgcolor: The label color of the TaskGroup node when displayed in
        the UI
    :type ui_fgcolor: str
    """

    def __init__(self, *args, **kwargs):

        print(kwargs)
        if "retries" in kwargs:
            self.retries = kwargs.pop("retries")
        else:
            self.retries = 3

        kwargs["ui_color"] = "#ed7864"
        kwargs["ui_fgcolor"] = "#fff"

        super(RetryTaskGroup, self).__init__(*args, **kwargs)

        if "dag" in kwargs:
            self.dag = kwargs["dag"]
        else:
            self.dag = DagContext.get_current_dag()
        self.old_args = dict(self.dag.default_args)

    def retry_all(self, context):
        ti = context["ti"]
        print("try {}".format(ti.try_number - 1))
        if ti.try_number > self.retries:
            print("mark failed")
            try:
                ti.error()
            except:
                pass
        else:
            print("retry these tasks:")
            for key, task in self.children.items():
                print(key)
                try:
                    task.clear(
                        start_date=context['execution_date'], end_date=context['execution_date'])
                except:
                    pass

    def add(self, task: BaseOperator) -> None:
        super(RetryTaskGroup, self).add(task)

    def __enter__(self):
        self.dag.default_args["on_retry_callback"] = self.retry_all
        self.dag.default_args["retries"] = self.retries
        super(RetryTaskGroup, self).__enter__()
        return self

    def __exit__(self, _type, _value, _tb):
        self.dag.default_args = self.old_args
        super(RetryTaskGroup, self).__exit__(_type, _value, _tb)








def get_fileApi():
    
    import eureka_requests

    import nest_asyncio
    nest_asyncio.apply()    
    
    import configparser
    config = configparser.ConfigParser()
    config.read('/defaults.cfg')

    url = config["VKfileapi"]["url"]
    token = config["VKfileapi"]["token"]

    fileApi = eureka_requests.RequestsApi(
        "FILE-SERVE-AZ",
        ":".join(url.split(":")[0:-1])+":8761",
        token
    )
    
    return fileApi



class UploadToAzure(BaseOperator):
    """
    Copy a file or more from the local filesystem to the Azure Blob Store
    """
    template_fields = ('templates_dict',)
    template_ext = tuple()
    ui_color = '#a8c5ff'

    # since we won't mutate the arguments, we should just do the shallow copy
    # there are some cases we can't deepcopy the objects(e.g protobuf).
    shallow_copy_attrs = ('python_callable', 'op_kwargs',)

    @apply_defaults
    def __init__(
            self,
            inputFile,
            outputFolder,
            location="sandbox",
            op_args=None,
            op_kwargs=None,
            templates_dict=None,
            templates_exts=None,
            *args, **kwargs):
        super(UploadToAzure, self).__init__(*args, **kwargs)
        self.op_args = op_args or []
        self.op_kwargs = op_kwargs or {}
        self.templates_dict = templates_dict
        if templates_exts:
            self.template_ext = templates_exts

        self.inputFile = inputFile if isinstance(inputFile, list) else [inputFile]
        self.outputFolder = outputFolder
        self.location = location

    def execute(self, context):
        return_value = self.execute_callable()
        self.log.info("Done. Returned value was: %s", return_value)
        return return_value

    def execute_callable(self):

        fileApi = get_fileApi()
        
        output = {}
        error = False
           
        for el in self.inputFile:
            filename = os.path.join(self.outputFolder, os.path.basename(el))
            
            res = fileApi.post(f"{self.location}/upload?filename={filename}", 
                               files={"file": open(el, "rb")}
                              )
            if res.ok:
                
                message = res.json()["message"]
                if message.startswith("Save"):
                    output[el] = "OK"
                else:
                    output[el] = message
                    error = True
            else:
                output[el] = "Error"
                error = True
 

        if error:
            raise Exception(json.dumps(output))
            
        return output


class DownloadFromAzure(BaseOperator):
    """
    Copy a file or more from  Azure to the local machine
    """
    template_fields = ('templates_dict',)
    template_ext = tuple()
    ui_color = '#899fcc'

    # since we won't mutate the arguments, we should just do the shallow copy
    # there are some cases we can't deepcopy the objects(e.g protobuf).
    shallow_copy_attrs = ('python_callable', 'op_kwargs',)

    @apply_defaults
    def __init__(
            self,
            inputFile,
            outputFolder,
            location="sandbox",
            op_args=None,
            op_kwargs=None,
            templates_dict=None,
            templates_exts=None,
            *args, **kwargs):
        super(DownloadFromAzure, self).__init__(*args, **kwargs)
        self.op_args = op_args or []
        self.op_kwargs = op_kwargs or {}
        self.templates_dict = templates_dict
        if templates_exts:
            self.template_ext = templates_exts

        self.inputFile = inputFile if isinstance(inputFile, list) else [inputFile]
        self.outputFolder = outputFolder
        self.location=location

    def execute(self, context):
        return_value = self.execute_callable()
        self.log.info("Done. Returned value was: %s", return_value)
        return return_value

    def execute_callable(self):

        fileApi = get_fileApi()
        
        output = {}
        error = False
           
        for el in self.inputFile:
            filename = os.path.basename(el)
            res = fileApi.post(f"{self.location}/load", json={"filename": el})
            if res.ok:
                with open(os.path.join(self.outputFolder, filename), "wb") as f:
                    f.write(res.content)
                    
                output[el] = "OK"
            else:
                output[el] = "Error"
                error = True
                
        if error:
            raise Exception(json.dumps(output))
        return output