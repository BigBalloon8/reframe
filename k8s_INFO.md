# Reframe with k8s

This documentation requires some basic familiarity with [reframe](https://reframe-hpc.readthedocs.io/en/stable/tutorial_basics.html)

Reframe for k8s is not designed to test Service resources or workload Deployments but individual workload resources such as Pods and Jobs.

Reframe for k8s is designed to run as a `RunOnlyRegressionTest` and will not execute anything provided to it outside of the workload config.

## Design Philosophy

There are 4 components used by reframe when running tests with k8s:

* Launch the k8s workload resource
* Wait for all pods associated with the workload resource to finish
* Write the logs of all the pods associated with the workload resource to the standard output 
* Delete the workload resource

We use a custom reframe scheduler to handle all of this. I'll show you an example of how this works.

## Example
First we have to change the scheduler and launcher in out system configuration here is the example used by the eidf's gpu-service:
```python
import socket

site_configuration = {
    "systems": [
        {
            "name": "eidf",
            "descr": "Edinburgh International Data Facility",
            "hostnames": [socket.gethostname()],
            "modules_system": "nomod",
            "partitions": [
                {
                    "name": "gpu-service",
                    "descr": "Edinburgh International Data Facility GPU-Service",
                    "scheduler": "k8s",
                    "launcher": "k8s",
                    "environs": ["Default"],
                },
            ],
        }
    ],
    "environments": [
        {
            "name": "Default",
            "target_systems": ["*"],
        },
    ],
    "logging": ...
}
```
As you can see the the `scheduler` has been set to `k8s`. Setting the `launcher` to `k8s` doesn't actually do anything its just a placeholder. Everything is controlled by the scheduler.

Next lets define our pod:

```yaml
#/path/to/cuda-pod.yml
apiVersion: v1
kind: Pod
metadata:
  generateName: 'cuda-test-pod'
  labels:
    kueue.x-k8s.io/queue-name:  eidf095ns-user-queue
spec:
  containers:
  - name: cudasample
    image: nvcr.io/nvidia/k8s/cuda-sample:nbody-cuda11.7.1
    args: ["-benchmark", "-numbodies=512000", "-fp64", "-fullscreen"]
    resources:
          requests:
              cpu: 2
              memory: '1Gi'
          limits:
              cpu: 2
              memory: '4Gi'
              nvidia.com/gpu: 1
  restartPolicy: Never
  nodeSelector:
    nvidia.com/gpu.product: NVIDIA-A100-SXM4-40GB
```
This is Example is a basic gpu benchmark provided by Nvidia.

Finally lets define out test, within the test we need to set `self.k8s_config` we have 2 option on how to do this.

```python
import reframe as rfm
import reframe.utility.sanity as sn

@rfm.simple_test
class CudaPodTest(rfm.RunOnlyRegressionTest):
    valid_systems = ['eidf:gpu-service']
    valid_prog_environs = ["*"]
    k8s_config = "/path/to/cuda-pod.yml"
```
OR 

```python
import reframe as rfm
import reframe.utility.sanity as sn

@rfm.simple_test
class CudaPodTest(rfm.RunOnlyRegressionTest):
    valid_systems = ['eidf:gpu-service']
    valid_prog_environs = ["*"]

    @run_after("init")
    def k8s_setup(self):
        k8s_config_path = "/path/to/cuda-pod.yml"
        with open(k8s_config_path, "r") as stream:
            pod_info = yaml.safe_load(stream)
        self.k8s_config = pod_info
```

In the first option we pass the path to the yaml defined above, this options trades usability for customization. The second option passes a python `dict` which contains the config defined in the yaml file. Let me show you an example of how the second option can be used to create a more customisable tests:

```python
# k8s_pod_test.py
import reframe as rfm
import reframe.utility.sanity as sn

@rfm.simple_test
class CudaPodTest(rfm.RunOnlyRegressionTest):
    valid_systems = ['eidf:gpu-service']
    valid_prog_environs = ["*"]
    
    num_bodies = parameter([1024000, 512000])

    @run_after("init")
    def k8s_setup(self):
        k8s_config_path = "/path/to/cuda-pod.yml"
        with open(k8s_config_path, "r") as stream:
            pod_info = yaml.safe_load(stream)
        pod_info["spec"]["containers"][0]["args"] = [
            "-benchmark", 
            f"-numbodies={self.num_bodies}", 
            "-fp64", 
            "-fullscreen"
        ]
        self.k8s_config = pod_info
    
    @performance_function("Iters/s", perf_key="Interactions per second")
    def extract_interactions_per_second(self):
        return sn.extractsingle(r"= (\d+\.\d+) billion interactions per second", self.stdout, tag= 1, conv=float)
    
    @performance_function("GLOP/s", perf_key="Flops")
    def extract_gflops(self):
        return sn.extractsingle(r"= (\d+\.\d+) double-precision GFLOP/s", self.stdout, tag= 1, conv=float)
    
    @sanity_function
    def assert_sanity(self):
        return sn.assert_found(r'double-precision GFLOP/s', filename=self.stdout)
    
    @run_before("performance")
    def set_perf_variables(self):
        self.perf_variables = {
            "Interactions per second": self.extract_interactions_per_second(),
            "Flops": self.extract_gflops(),
        }
```
```bash
export REFRAME_CONFIG=/path/to/reframe_config
reframe -C ${REFRAME_CONFIG} -c k8s_pod_test.py -r --performance-report
```

This will launch 2 k8s pods as 2 separate reframe tests, the first with numbodies being 512,000 and the other 1,024,000. I added the performance functions and sanity check to the more customised test in order to generate a better output but they could also be used with the first option.

It important to note that Pods aren't the only k8s workload resource that are supported Jobs are also available the detail on how, are documented bellow. Other custom workload resources types are available as an experimental feature.

Now lets walk through what the scheduler is doing behind the scenes.

## Behind the Scenes

Everything happens during the "Run Phase" of the regression test. 

First the scheduler reads the k8s_config and updates all of the metadata leafs found in the config. If the metadata contains a `name` object then the `name` will be updates as follow `f"rfm-{config[...]["metadata"]["name"]}-{identifier}"` if the metadata contains a `generateName` object then the `name` will be update as follows `f"rfm-{config[...]["metadata"]["generateName"]}{identifier}"` and the original `"generateName"` will be removed from the metadata. The `identifier` variable is a 8 character long random string that is unique to each reframe test used to identify and keep track of the workload resources by the scheduler.
`{"rfm":identifier}` is also appended to the metadata's labels and is used to identify both which workload resources are associated with reframe and which are associated with the specific test allowing for multiple test to run at once. 

Next the k8s workload resource is launched, think of this as the equivalent of running `kubectl create -f /path/to/k8s_config`. In some cases this is actually what happens but we try use the k8s python api as much as possible to better deal with API error handling. In the case of a `Pod` workload resource we will launch it directly with the python api, this allows us to deal with situations such as multiple tests causing your k8s namespace quota to exceeded the limits. We are then able to delay execution of tests until resources become available due to better error handling.

When the workload resource is launched a logging thread will be generated. The job of this thread is to write the output of all the pods associated with the workload resource to the stdout of the test until the pod has either succeeded, failed, or crashed. The thread can identify which pods are associated with the given test by the unique `identifier` we added to the metadata of the pod during launch.

Once the workload resource has been launched the scheduler will wait for one of three things:
1. All pods associated with the workload resource of the test either succeed fail or crash
2. The tests time limit is reached
3. The user cancels the test

Once one of the above events has happened the scheduler will check to see if all the pods completed successfully, if so the scheduler will wait for the logging thread to complete and clean up all the workload. if not all the pods where successful the scheduler will close the logging thread dump the logs to the stdout and **will not** cleanup the workload resources allowing the user to look at the pods/workload and assess the problem. If the user cancels the test the scheduler will close the logging thread dump the logs to the stdout and **will** cleanup the workload resources.

Its important to understand that both the stdout and stderr of the pods are written to the stdout file of the test. The reason for this is that the only way to access the outputs of the pods is with `kubectl logs PODNAME` or as is done with reframe, the equivalent with the python api. These commands provided both stdout and stderr as a single output thus causing this problem. The reason this matters is that when writing your performance functions or sanity checks you shouldn't read from the stderr as it will be empty.

### ###Note###
The metadata name changes may be removed as they are no longer used to identify pods, as was the case in a previous version.

## Jobs
The process of launching a Job is the exact same as a Pod, I'll show you an example:

First lets define our Job's yaml:
```yaml
apiVersion: batch/v1
kind: Job
metadata:
    generateName: jobtest-
    labels:
        kueue.x-k8s.io/queue-name:  eidf095ns-user-queue
spec:
    completions: 3
    parallelism: 2
    template:
        metadata:
            name: job-test
        spec:
            containers:
            - name: cudasample
              image: nvcr.io/nvidia/k8s/cuda-sample:nbody-cuda11.7.1
              args: ["-benchmark", "-numbodies=512000", "-fp64", "-fullscreen"]
              resources:
                    requests:
                        cpu: 2
                        memory: '1Gi'
                    limits:
                        cpu: 2
                        memory: '4Gi'
                        nvidia.com/gpu: 1
            restartPolicy: Never
            nodeSelector:
                nvidia.com/gpu.product: NVIDIA-A100-SXM4-40GB
```
This test runs the exact same benchmark as the k8s Pod example. The difference is that the job will run the 3 pods with the option of 2 pods being able to run in parallel.

Next lets define our test:

```python
# k8s_job_test.py

@rfm.simple_test
class CudaJodTest(rfm.RunOnlyRegressionTest):
    valid_systems = ['eidf:gpu-service']
    valid_prog_environs = ["*"]
    k8s_config = "/path/to/cuda-job.yml"
    
    reference = {
        "eidf:gpu-service": {
            "Interactions per second": (250, -0.1, 0.1, "Iters/s"),
            "Flops": (7440, -0.1, 0.1, "GLOP/s"),
        }
    }
    
    @performance_function("Iters/s", perf_key="Interactions per second")
    def extract_interactions_per_second(self):
        return sn.extractsingle(r"= (\d+\.\d+) billion interactions per second", self.stdout, tag= 1, conv=float)
    
    @performance_function("GLOP/s", perf_key="Flops")
    def extract_gflops(self):
        return sn.extractsingle(r"= (\d+\.\d+) double-precision GFLOP/s", self.stdout, tag= 1, conv=float)
    
    @sanity_function
    def assert_sanity(self):
        num_messages = sn.len(sn.findall(r'double-precision GFLOP/s', filename=self.stdout))
        return sn.assert_eq(num_messages, 3)    
    
    @run_before("performance")
    def set_perf_variables(self):
        self.perf_variables = {
            "Interactions per second": self.extract_interactions_per_second(),
            "Flops": self.extract_gflops(),
        }
```

The process of passing the workload config to k8s_config is the exact same as the pod test.

Behind the scenes the scheduler will dynamically extract the workload resource's kind, If the scheduler detects that the workload resource's kind is a Job, the scheduler will read the `completions` value found in job spec or set it to 1 if its not provided. The scheduler will then wait for the number of completions worth of pods associated with that test to finish.

## stdout

Understanding the structure of the stdout of the test is important when writing performance functions and sanity checks. The structure of a stdout of a workload resource with N pods associated with it is as follows:
```
Identifier
-----Pod 0 Name-----
...
-----Pod 0 Log-----
...

-----Pod n Name-----
...
-----Pod n Log-----
...

-----Pod N-1 Name-----
...
-----Pod N-1 Log-----
...
```

Here is the output of the test in the example above with 3 pods.

```diff
aotugiob
-------rfm-jobtest-aotugiob-6xdms-------
Run "nbody -benchmark [-numbodies=<numBodies>]" to measure performance.
	-fullscreen       (run n-body simulation in fullscreen mode)
	-fp64             (use double precision floating point values for simulation)
	-hostmem          (stores simulation data in host memory)
	-benchmark        (run benchmark to measure performance) 
	-numbodies=<N>    (number of bodies (>= 1) to run in simulation) 
	-device=<d>       (where d=0,1,2.... for the CUDA device to use)
	-numdevices=<i>   (where i=(number of CUDA devices > 0) to use for simulation)
	-compare          (compares simulation results running once on the default GPU and once on the CPU)
	-cpu              (run n-body simulation on the CPU)
	-tipsy=<file.bin> (load a tipsy model file for simulation)

NOTE: The CUDA Samples are not meant for performance measurements. Results may vary when GPU Boost is enabled.

> Fullscreen mode
> Simulation data stored in video memory
> Double precision floating point simulation
> 1 Devices used for simulation
GPU Device 0: "Ampere" with compute capability 8.0

> Compute 8.0 CUDA device: [NVIDIA A100-SXM4-40GB]
number of bodies = 512000
512000 bodies, total time for 10 iterations: 10570.772 ms
= 247.989 billion interactions per second
= 7439.683 double-precision GFLOP/s at 30 flops per interaction

-------rfm-jobtest-aotugiob-97w5g-------
Run "nbody -benchmark [-numbodies=<numBodies>]" to measure performance.
	-fullscreen       (run n-body simulation in fullscreen mode)
	-fp64             (use double precision floating point values for simulation)
	-hostmem          (stores simulation data in host memory)
	-benchmark        (run benchmark to measure performance) 
	-numbodies=<N>    (number of bodies (>= 1) to run in simulation) 
	-device=<d>       (where d=0,1,2.... for the CUDA device to use)
	-numdevices=<i>   (where i=(number of CUDA devices > 0) to use for simulation)
	-compare          (compares simulation results running once on the default GPU and once on the CPU)
	-cpu              (run n-body simulation on the CPU)
	-tipsy=<file.bin> (load a tipsy model file for simulation)

NOTE: The CUDA Samples are not meant for performance measurements. Results may vary when GPU Boost is enabled.

> Fullscreen mode
> Simulation data stored in video memory
> Double precision floating point simulation
> 1 Devices used for simulation
GPU Device 0: "Ampere" with compute capability 8.0

> Compute 8.0 CUDA device: [NVIDIA A100-SXM4-40GB]
number of bodies = 512000
512000 bodies, total time for 10 iterations: 10572.246 ms
= 247.955 billion interactions per second
= 7438.646 double-precision GFLOP/s at 30 flops per interaction

-------rfm-jobtest-aotugiob-jfbcd-------
Run "nbody -benchmark [-numbodies=<numBodies>]" to measure performance.
	-fullscreen       (run n-body simulation in fullscreen mode)
	-fp64             (use double precision floating point values for simulation)
	-hostmem          (stores simulation data in host memory)
	-benchmark        (run benchmark to measure performance) 
	-numbodies=<N>    (number of bodies (>= 1) to run in simulation) 
	-device=<d>       (where d=0,1,2.... for the CUDA device to use)
	-numdevices=<i>   (where i=(number of CUDA devices > 0) to use for simulation)
	-compare          (compares simulation results running once on the default GPU and once on the CPU)
	-cpu              (run n-body simulation on the CPU)
	-tipsy=<file.bin> (load a tipsy model file for simulation)

NOTE: The CUDA Samples are not meant for performance measurements. Results may vary when GPU Boost is enabled.

> Fullscreen mode
> Simulation data stored in video memory
> Double precision floating point simulation
> 1 Devices used for simulation
GPU Device 0: "Ampere" with compute capability 8.0

> Compute 8.0 CUDA device: [NVIDIA A100-SXM4-40GB]
number of bodies = 512000
512000 bodies, total time for 10 iterations: 10570.343 ms
= 248.000 billion interactions per second
= 7439.986 double-precision GFLOP/s at 30 flops per interaction

```

## Additional Options

Reframe also allows you to specify which namespace and context to use. By default you namespace will be set to the `"default"` namespace and the context will use the current context defined in the `KUBECONFIG`. Here is how you set both:
```python
@rfm.simple_test
class CudaJodTest(rfm.RunOnlyRegressionTest):
    valid_systems = ['eidf:gpu-service']
    valid_prog_environs = ["*"]
    k8s_config = "/path/to/cuda-job.yml"

    namespace = "NAMESPACE"
    context = "CONTEXT"
```
## Known Errors
1. Do not set the `random.seed(x)` within a test that is [parameterized](https://reframe-hpc.readthedocs.io/en/stable/tutorial_advanced.html#parameterizing-a-regression-test), this will cause all of the test to have the same identifier.

2. If the terminal running the reframe test is closed the k8s workload's resources will continue running and not clean up or write to stdout on completion. 

3. If there is a resource limit in your namespace, the workload resources may not launch if the quota is exceeded but reframe will not be aware and will deadlock by waiting for the workload resources to launch. If the workload resource is a Pod, reframe is able to dynamically detect when the limit has been exceeded and either wait for resource to become available or raise an exception if the pod is asking for a number of resources that completely exceed the quota.

If any other issues are found please contact me at [crae@ed.ac.uk](mailto:crae@ed.ac.uk?subject=Reframe%20k8s%20Bug)

## Custom Workloads (**EXPERIMENTAL!!!**)

This example will walk through how to implement a reframe test for a Graphcore k8s [IPU job](https://docs.graphcore.ai/projects/kubernetes-user-guide/en/1.1.0/creating-ipujob.html). Graphcore is a startup that have created a custom Processor designed for ML called an IPU.

First lets define our k8s_config:
```yaml
# /path/to/ipu_job.yml
apiVersion: graphcore.ai/v1alpha1
kind: IPUJob
metadata:
  generateName: mnist-training-
spec:
  # jobInstances defines the number of job instances.
  # More than 1 job instance is usually useful for inference jobs only.
  jobInstances: 1
  # ipusPerJobInstance refers to the number of IPUs required per job instance.
  # A separate IPU partition of this size will be created by the IPU Operator
  # for each job instance.
  ipusPerJobInstance: "1"
  workers:
    template:
      spec:
        containers:
        - name: mnist-training
          image: graphcore/pytorch:3.3.0
          command: [/bin/bash, -c, --]
          args:
            - |
              cd;
              mkdir build;
              cd build;
              git clone https://github.com/graphcore/examples.git;
              cd examples/tutorials/simple_applications/pytorch/mnist;
              python -m pip install -r requirements.txt;
              python mnist_poptorch_code_only.py --epochs 1
          resources:
            limits:
              cpu: 32
              memory: 200Gi
          securityContext:
            capabilities:
              add:
              - IPC_LOCK
          volumeMounts:
          - mountPath: /dev/shm
            name: devshm
        restartPolicy: Never
        hostIPC: true
        volumes:
        - emptyDir:
            medium: Memory
            sizeLimit: 10Gi
          name: devshm
```

This benchmark will train a simple DNN on the mnsit dataset using 1 graphcore IPU. The k8s config in this case is very similar to a regular k8s Job. The reason we cant just use this directly with reframe is due to the way we interact with the ipujob. Instead of `kubectl get job` we must use `kubectl get ipujob` (our resource type has changed). To get around this we must pass our new reasource type to reframe:

```python
@rfm.simple_test
class IPUTest(rfm.RunOnlyRegressionTest):
    valid_systems = ['eidf:graphcore']
    valid_prog_environs = ["*"]
    k8s_config = "/path/to/ipu_job.yml"
    k8s_resource = "ipujob"

    ...
```
`'eidf:graphcore'` has the exact same config as the `'eidf:gpu-service'` defined above.

The value we pass in as `k8s_resource` is the value you would use to query the resource (e.g. `kubectl get {k8s_resource}`). The ipujob above will only have 1 pod associated with it defined here `ipusPerJobInstance: "1"`, as of the current release custom workload resources with more than 1 pod are not supported due to reframe not having a way to extract the number of pods associated with custom workload's config. A safe work around for this is to use a paramaterised test were the parameter doestn change a variable that effect the test:

```python
@rfm.simple_test
class IPUTest(rfm.RunOnlyRegressionTest):
    valid_systems = ['eidf:graphcore']
    valid_prog_environs = ["*"]
    k8s_resource = "ipujob"
    n = parameter([1,2,3])

    @run_after("init")
    def k8s_setup(self):
        k8s_config_path = "/path/to/ipu_job.yml"
        with open(k8s_config_path, "r") as stream:
            job_info = yaml.safe_load(stream)
        job_info["metadata"]["generateName"] = f"{job_info["metadata"]["generateName"]}{self.n}-"
        self.k8s_config = job_info
```
OR (you can add parameters that dont do anything, make sure the parameters have different values otherwise reframe will give every test the same name causing problems with logging and stdout/stderr)
```python
@rfm.simple_test
class IPUTest(rfm.RunOnlyRegressionTest):
    valid_systems = ['eidf:graphcore']
    valid_prog_environs = ["*"]
    k8s_resource = "ipujob"
    k8s_config = "/path/to/ipu_job.yml"
    n = parameter([1,2,3])
```