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
                        return context["context"]["namespace"]
            else:
                return "default"
    return namespace


def launch_k8s(namespace=None, context=None, k8s_def: dict="", std_out="", workload_type=None):
    namespace = _get_name_space(namespace)

    if isinstance(k8s_def, str):
        with open(k8s_def, "r") as stream:
            k8s_def = yaml.safe_load(stream)
    
    base_identifier =''.join(random.choices(string.ascii_lowercase, k=8))
    metadata_handler(k8s_def, base_identifier)

    if k8s_def["kind"] == "Pod":
        num_pods = 1
        workload_type = "pod"
        _launch_pod(namespace, context, k8s_def, std_out)
    elif k8s_def["kind"] == "Job":
        if "completions" in k8s_def["spec"].keys():
            num_pods = k8s_def["spec"]["completions"]
        else:
            num_pods = 1
        workload_type = "job"
        _launch_custom(namespace, context, k8s_def, std_out)
    elif workload_type != None:
        num_pods = 1
        _launch_custom(namespace, context, k8s_def, std_out)
    else:
        raise NotImplementedError(f"K8s Type not supported: {k8s_def['type']}, currently only Pod & Job are supported contact crae@ed.ac.uk yo ")
    
    log_thread = multiprocessing.Process(target=_logging_thread, args=(namespace, context, base_identifier, std_out, num_pods,))
    log_thread.start()
    return namespace, log_thread, num_pods, workload_type, base_identifier


def metadata_handler(container, identifier:str):
    if isinstance(container, dict):
        for k in container.keys():
            if k == "metadata":
                #Label
                if "labels" in container[k].keys():
                    container[k]["labels"]["rfm"] = identifier
                else:
                    container[k]["labels"] = {"rfm": identifier}

                #Name
                if "name" in container[k].keys():
                    container[k]["name"] = "rfm-" + container[k]["name"] + f"-{identifier}"
                elif "generateName" in container[k].keys():
                    container[k]["name"] = "rfm-" + container[k]["generateName"] + f"{identifier}"
                    del container[k]["generateName"]
            else:
                if k == "template":
                    if "metadata" not in container[k].keys():
                        container[k]["metadata"] = {}
                metadata_handler(container[k], identifier)
    elif isinstance(container, list):
        for i in container:
            metadata_handler(i, identifier)


def get_pods(identifier, namespace):
    core_v1 = client.CoreV1Api()
    pods = core_v1.list_namespaced_pod(namespace=namespace, label_selector=f"rfm={identifier}")
    return pods.items

    
def _logging_thread(namespace, context, identifier, std_out, num_pods):
    config.load_kube_config(os.environ["KUBECONFIG"], context=context)
    v1 = client.CoreV1Api()
    pod_names = []
    n_complete = 0
    pods = get_pods(identifier, namespace)
    while n_complete != num_pods:
        pods = get_pods(identifier, namespace)
        for pod in pods:
            if pod.status.phase in ("Failed", "Succeeded","CrashLoopBackOff") and pod.metadata.name not in pod_names:
                n_complete += 1
                pod_names.append(pod.metadata.name)
        time.sleep(0.5)
    
    with open(std_out, "a+") as std_out_file:
        std_out_file.write(f"{identifier}\n")
        for name in pod_names:
            std_out_file.write(f"\n-------{name}-------\n")
            std_out_file.write(v1.read_namespaced_pod_log(name=name, namespace=namespace))

def _dump_logs(namespace, context, identifier, std_out):
    config.load_kube_config(os.environ["KUBECONFIG"], context=context)
    v1 = client.CoreV1Api()
    pod_names = []
    pods = get_pods(identifier, namespace)
    for pod in pods:
        pod_names.append(pod.metadata.name)

    with open(std_out, "a+") as std_out_file:
        std_out_file.write(f"{identifier}\n")
        for name in pod_names:
            std_out_file.write(f"\n-------{name}-------\n")
            std_out_file.write(v1.read_namespaced_pod_log(name=name, namespace=namespace))
    

def _has_finished(identifier, namespace, num_pods, context=None):
    config.load_kube_config(os.environ["KUBECONFIG"], context=context)
    statuses = []
    for pod in get_pods(identifier, namespace):
        statuses.append(pod.status.phase in ("Failed", "Succeeded", "CrashLoopBackOff"))
    return all(statuses) and len(statuses) == num_pods

def _pod_failed(identifier, namespace, num_pods, context=None):
    config.load_kube_config(os.environ["KUBECONFIG"], context=context)
    for pod in get_pods(identifier, namespace):
        if pod.status.phase == "Failed":
            return True
    return False
    

def _all_success(identifier, namespace, context=None):
    config.load_kube_config(os.environ["KUBECONFIG"], context=context)
    statuses = []
    for pod in get_pods(identifier, namespace):
        statuses.append(pod.status.phase == "Succeeded")
    return all(statuses)

def _delete_workload(identifier, namespace, resource, context=None):
    if context is None:
        osext.run_command(f"kubectl delete {resource} --selector='rfm={identifier}' -n {namespace}", check=True)
    else:
        osext.run_command(f"kubectl delete {resource} --context={context} --selector='rfm={identifier}' -n {namespace}", check=True)


#--------------------
# POD
#--------------------


def _launch_pod(namespace=None, context=None, pod_def: dict="", std_out=""):
    # Init Config
    config.load_kube_config(os.environ["KUBECONFIG"], context=context)
    v1 = client.CoreV1Api()

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


#--------------------
# Custom
#--------------------                    

def _launch_custom(namespace=None, context=None, job_def: dict={}, std_out=""):

    stage_dir = os.path.dirname(std_out)
    with open(os.path.join(stage_dir, "k8s_def.yaml"), "w") as stream:
        yaml.dump(job_def, stream)
    
    # TODO add logic for resource limits
    if context is None:
        osext.run_command(f"kubectl create -f {os.path.join(stage_dir, 'k8s_def.yaml')} -n {namespace}", check=True)
    else:
        osext.run_command(f"kubectl create -f {os.path.join(stage_dir, 'k8s_def.yaml')} --context={context} -n {namespace}", check=True)

    