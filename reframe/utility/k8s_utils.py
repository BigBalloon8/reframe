import time
import yaml
import json
import re
import os
import random
import string
import warnings
import multiprocessing
import threading

from kubernetes import client, config
from kubernetes.client.rest import ApiException

import reframe.utility.osext as osext

def _get_name_space(namespace=None):
    if namespace is None:
        with open(os.path.expanduser(os.environ["KUBECONFIG"]), "r") as stream:
            k_config = yaml.safe_load(stream)
            if "current-context" in k_config.keys():
                cur_con = k_config["current-context"]
                for context in k_config["contexts"]:
                    if context["name"] == cur_con:
                        namespace = context["context"]["namespace"]
                        break
            else:
                try: 
                    namespace = k_config["contexts"][0]["context"]["namespace"]
                except (IndexError, KeyError):
                    raise ValueError("Could not find namespace please provide with --namespace ________ to executable_opts")
    return namespace

def launch_k8s(namespace=None, k8s_def: dict="", std_out=""):
    if isinstance(k8s_def, str):
        with open(k8s_def, "r") as stream:
            k8s_def = yaml.safe_load(stream)

    if k8s_def["kind"] == "Pod":
        return *_launch_pod(namespace, k8s_def, std_out), "pod"
    elif k8s_def["kind"] == "Job":
        return *_launch_job(namespace, k8s_def, std_out), "job"
    else:
        raise NotImplementedError(f"K8s Type not supported: {k8s_def['type']}, currently only Pod & Job are supported contact crae@ed.ac.uk yo ")

#--------------------
# POD
#--------------------

def _get_pod_name(base_pod_name, namespace):
    v1 = client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace)
    pod_names = [pod.metadata.name for pod in pods.items]
    for pod_name in pod_names:
        if base_pod_name in pod_name:
            return pod_name


def _pod_logging_thread(namespace, pod_name, std_out):
    v1 = client.CoreV1Api()
    status = ""
    start_idx = 0
            
    with open(std_out, "a+") as std_out_file:
        while status not in ("Failed", "Succeeded", "Terminating"):
            try:
                status = v1.read_namespaced_pod(name=pod_name, namespace=namespace).status.phase
                pod_log = v1.read_namespaced_pod_log(name=pod_name, namespace=namespace)
                if pod_log[start_idx:]:
                    std_out_file.write(pod_log[start_idx:])
                    start_idx = len(pod_log)
            except ApiException as e:
                pass
            time.sleep(0.5)


def _launch_pod(namespace=None, pod_def: dict="", std_out=""):
    # Get Namespace
    namespace = _get_name_space(namespace)
    # Init Config
    config.load_kube_config(os.environ["KUBECONFIG"])
    v1 = client.CoreV1Api()
    # Load pod_def
    if isinstance(pod_def, str):
        with open(pod_def, "r") as stream:
            pod_def = yaml.safe_load(stream)
    
    # Change Names to support multiple pods
    base_identifier =''.join(random.choices(string.ascii_lowercase, k=8))

    if "generateName" in pod_def["metadata"].keys():
        base_pod_name = pod_def["metadata"]["generateName"]
        base_pod_name  = f"rfm-{base_pod_name}-{base_identifier}"
        pod_def["metadata"]["name"] = base_pod_name
        pod_def["metadata"].pop("generateName")
    elif "name" in pod_def["metadata"].keys():
        base_pod_name = pod_def["metadata"]["name"]
        base_pod_name  = f"rfm-{base_pod_name}{base_identifier}"
        pod_def["metadata"]["name"] = base_pod_name
    
    container_types = ["containers", "initContainers", "ephemeralContainers"]
    for container_type in container_types:
        if container_type in pod_def["spec"].keys():
            for i in range(len(pod_def["spec"][container_type])):
                container_name = pod_def["spec"][container_type][i]["name"]
                container_name = f"{container_name}-{base_identifier}"
                pod_def["spec"][container_type][i]["name"] = container_name
                
    stage_dir = os.path.dirname(std_out)
    with open(os.path.join(stage_dir, "k8s_def.yaml"), "w") as stream:
        yaml.dump(pod_def, stream)
    # Launch Pod
    while True:
        try:
            v1.create_namespaced_pod(body=pod_def, namespace=namespace)
        except ApiException as e:
            body = json.loads(e.body)["message"]
            if "exceeded quota:" in body:
                pattern_requested = r"requested:.*?(\d+)"
                pattern_used = r"used:.*?(\d+)"
                pattern_limited = r"limited:.*?(\d+)"

                resource_pattern = r"requested: requests\.([\w\.\/]+)=\d+"
                resource = re.search(resource_pattern, body).group(1)

                requested = re.search(pattern_requested, body).group(1)
                used = re.search(pattern_used, body).group(1)
                limited = re.search(pattern_limited, body).group(1)
                if int(requested) > int(limited):
                    raise ValueError(f"Namespace {namespace} is limited to {limited} but requested {requested}")
                warnings.warn(f"Maximum quota of {resource} in use waiting for resources to free up (in use: {used}, requested: {requested}, limit: {limited})")
                time.sleep(10)
                continue
            #if "higher than the maximum allowed" in body:
            else:
                raise e
        break
    stage_dir = os.path.dirname(std_out)
    with open(os.path.join(stage_dir, "k8s_def.yaml"), "w") as stream:
        yaml.dump(pod_def, stream)
        
    log_thread = multiprocessing.Process(target=_pod_logging_thread, args=(namespace, _get_pod_name(base_pod_name, namespace), std_out,))
    log_thread.start()
    # TODO allow time for pod to be inited
    return _get_pod_name(base_pod_name, namespace), namespace, log_thread, 1


def _delete_pod(pod_name, namespace):
    v1 = client.CoreV1Api()
    v1.delete_namespaced_pod(name=pod_name, namespace=namespace)


def _has_pod_finished(pod_name, namespace):
    v1 = client.CoreV1Api()
    return v1.read_namespaced_pod(name=pod_name, namespace=namespace).status.phase in ("Failed", "Succeeded","CrashLoopBackOff")


#--------------------
# JOB
#--------------------


def _get_all_pods(base_name, namespace):
    v1 = client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace)
    pod_names = [pod.metadata.name for pod in pods.items]
    return [pod_name for pod_name in pod_names if base_name in pod_name]


def _job_logging_thread(namespace, job_name, std_out, num_pods):
    v1 = client.CoreV1Api()
    
    pod_names = []
    pods = v1.list_namespaced_pod(namespace, label_selector=f"job-name={job_name}")
    while not(len(pod_names) == num_pods and all([pod.status.phase in ("Failed", "Succeeded", "CrashLoopBackOff") for pod in pods.items])):
        pods = v1.list_namespaced_pod(namespace,
                                  label_selector=f"job-name={job_name}")
        for pod in pods.items:
            name = pod.metadata.name
            if not name in pod_names:
                pod_names.append(name)
        time.sleep(1)

    time.sleep(1)
    
    with open(std_out, "a+") as std_out_file:
        std_out_file.write(f"{job_name}\n")
        for name in pod_names:
            std_out_file.write(f"\n-------{name}-------\n")
            std_out_file.write(v1.read_namespaced_pod_log(name=name, namespace=namespace))
                    
    

def _launch_job(namespace=None, job_def: dict="", std_out=""):
    # Get Namespace
    namespace = _get_name_space(namespace)
    # Init Config
    config.load_kube_config(os.environ["KUBECONFIG"])


    if isinstance(job_def, str):
        with open(job_def, "r") as stream:
            job_def = yaml.safe_load(stream)

    base_identifier =''.join(random.choices(string.ascii_lowercase, k=8))

    if "generateName" in job_def["metadata"].keys():
        base_job_name = job_def["metadata"]["generateName"]
        base_job_name  = f"rfm-{base_job_name}-{base_identifier}"
        job_def["metadata"]["name"] = base_job_name
        job_def["metadata"].pop("generateName")
    elif "name" in job_def["metadata"].keys():
        base_job_name = job_def["metadata"]["name"]
        base_job_name  = f"rfm-{base_job_name}{base_identifier}"
        job_def["metadata"]["name"] = base_job_name
    

    if "generateName" in job_def["spec"]["template"]["metadata"].keys():
        base_pod_name = job_def["spec"]["template"]["metadata"]["generateName"]
        base_pod_name  = f"rfm-{base_pod_name}-{base_identifier}"
        job_def["spec"]["template"]["metadata"]["name"] = base_pod_name
        job_def["spec"]["template"]["metadata"].pop("generateName")
    elif "name" in job_def["spec"]["template"]["metadata"].keys():
        base_pod_name = job_def["spec"]["template"]["metadata"]["name"]
        base_pod_name  = f"rfm-{base_pod_name}{base_identifier}"
        job_def["spec"]["template"]["metadata"]["name"] = base_pod_name
    
    
    container_types = ["containers", "initContainers", "ephemeralContainers"]
    for container_type in container_types:
        if container_type in job_def["spec"]["template"]["spec"].keys():
            for i in range(len(job_def["spec"]["template"]["spec"][container_type])):
                container_name = job_def["spec"]["template"]["spec"][container_type][i]["name"]
                container_name = f"{container_name}-{base_identifier}"
                job_def["spec"]["template"]["spec"][container_type][i]["name"] = container_name
    
    stage_dir = os.path.dirname(std_out)
    with open(os.path.join(stage_dir, "k8s_def.yaml"), "w") as stream:
        yaml.dump(job_def, stream)
        
    if "completions" in job_def["spec"].keys():
        num_pods = job_def["spec"]["completions"]
    else:
        num_pods = 1
    
    # TODO add logic for resource limits
    osext.run_command(f"kubectl create -f {os.path.join(stage_dir, 'k8s_def.yaml')} -n {namespace}")
    
    log_thread = threading.Thread(target=_job_logging_thread, args=(namespace, base_job_name, std_out, num_pods,))
    log_thread.start()
    return base_job_name, namespace, log_thread, num_pods

def _delete_job(job_name, namespace):
    osext.run_command(f"kubectl delete job {job_name} -n {namespace}")

def _has_job_finished(base_job_name, namespace, num_pods):
    v1 = client.CoreV1Api()
    statuses = []
    for pod_name in _get_all_pods(base_job_name, namespace):
        statuses.append(v1.read_namespaced_pod(name=pod_name, namespace=namespace).status.phase in ("Failed", "Succeeded", "CrashLoopBackOff"))
    return all(statuses) and len(statuses) == num_pods
