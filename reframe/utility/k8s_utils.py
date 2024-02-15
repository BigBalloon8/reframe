import time
import yaml
import json
import re
import os
import random
import string
import warnings
import multiprocessing

from kubernetes import client, config
from kubernetes.client.rest import ApiException

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

def _get_pod_name(base_pod_name, namespace):
    v1 = client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace)
    pod_names = [pod.metadata.name for pod in pods.items]
    for pod_name in pod_names:
        if base_pod_name in pod_name:
            return pod_name

def logging_thread(namespace, pod_name, std_out):
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

def launch_pod(namespace=None, pod_def: dict="", std_out=""):
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
    if "metadata" in pod_def.keys():
        if "generateName" in pod_def["metadata"].keys():
            base_pod_name = pod_def["metadata"]["generateName"]
            base_pod_name  = f"rfm-{base_pod_name}-{base_identifier}"
            pod_def["metadata"]["name"] = base_pod_name
            pod_def["metadata"].pop("generateName")
        elif "name" in pod_def["metadata"].keys():
            base_pod_name = pod_def["metadata"]["name"]
            base_pod_name  = f"rfm-{base_pod_name}{base_identifier}"
            pod_def["metadata"]["name"] = base_pod_name
        else:
            raise ValueError("Pod Definition must have a name or generateName")
    else:
        pod_def["metadata"] = {"name": f"rfm-{base_identifier}"}
    
    container_types = ["containers", "initContainers", "ephemeralContainers"]
    for container_type in container_types:
        if "template" in pod_def["spec"].keys():
            if container_type in pod_def["spec"]["template"]["spec"].keys():
                for i in range(len(pod_def["spec"]["template"]["spec"][container_type])):
                    container_name = pod_def["spec"]["template"]["spec"][container_type][i]["name"]
                    container_name = f"{container_name}-{base_identifier}"
                    pod_def["spec"]["template"]["spec"][container_type][i]["name"] = container_name
        else:
            if container_type in pod_def["spec"].keys():
                for i in range(len(pod_def["spec"][container_type])):
                    container_name = pod_def["spec"][container_type][i]["name"]
                    container_name = f"{container_name}-{base_identifier}"
                    pod_def["spec"][container_type][i]["name"] = container_name
    
    # Launch Pod
    while True:
        try:
            v1.create_namespaced_pod(body=pod_def, namespace=namespace)
        except ApiException as e:
            body = json.loads(e.body)["message"]
            # TODO Make this work for all reasources
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
    log_thread = multiprocessing.Process(target=logging_thread, args=(namespace, _get_pod_name(base_pod_name, namespace), std_out,))
    log_thread.start()
    # TODO allow time for pod to be inited
    return _get_pod_name(base_pod_name, namespace), namespace, log_thread

def delete_pod(pod_name, namespace):
    v1 = client.CoreV1Api()
    v1.delete_namespaced_pod(name=pod_name, namespace=namespace)

def has_finished(pod_name, namespace):
    v1 = client.CoreV1Api()
    return v1.read_namespaced_pod(name=pod_name, namespace=namespace).status.phase in ("Failed", "Succeeded","CrashLoopBackOff")

def wait_for_pod_completion(base_pod_name):
    namespace = _get_name_space(namespace)
    pod = _get_pod_name(base_pod_name, namespace)
    v1 = client.CoreV1Api()
    status = ""
    while status not in ("Failed", "Succeeded", "CrashLoopBackOff"):
        status = v1.read_namespaced_pod(name=pod, namespace=namespace).status.phase
        time.sleep(0.5)
    
        