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
        while status not in ("Failed", "Succeeded"):
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
    
    # Change Name to support multiple pods
    if "metadata" in pod_def.keys():
        if "generateName" in pod_def["metadata"].keys():
            base_pod_name = pod_def["metadata"]["generateName"]
            base_pod_name  = f"rfm-{base_pod_name}{''.join(random.choices(string.ascii_lowercase, k=8))}"
            pod_def["metadata"]["name"] = base_pod_name
            pod_def["metadata"].pop("generateName")
        elif "name" in pod_def["metadata"].keys():
            base_pod_name = pod_def["metadata"]["name"]
            base_pod_name  = f"rfm-{base_pod_name}{''.join(random.choices(string.ascii_lowercase, k=8))}"
            pod_def["metadata"]["name"] = base_pod_name
        else:
            raise ValueError("Pod Definition must have a name or generateName")
    else:
        pod_def["metadata"] = {"name": f"rfm-{''.join(random.choices(string.ascii_lowercase, k=8))}"}
    
    for i in range(len(pod_def["spec"]["containers"])):
        container_name = pod_def["spec"]["containers"][i]["name"]
        container_name = f"{container_name}-{''.join(random.choices(string.ascii_lowercase, k=8))}"
        pod_def["spec"]["containers"][i]["name"] = container_name
    
    # Launch Pod
    while True:
        try:
            v1.create_namespaced_pod(body=pod_def, namespace=namespace)
        except ApiException as e:
            body = json.loads(e.body)["message"]
            # TODO Make this work for all reasources
            if "exceeded quota:" in body:
                if "gpu" not in body:
                    raise ValueError(f"Quota Exceeded See: {e.body}")
                num_gpus = pod_def["spec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"]
                limit = re.search(r'limited: requests.*=(\d+)', body)
                if num_gpus > int(limit.group(1)):
                    raise ValueError(f"Requested {num_gpus} GPU, namespace limited to {limit.group(1)} GPUs")
                warnings.warn("Maximum quota resources in use waiting for resources to free up")
                time.sleep(10)
                continue
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
    
        