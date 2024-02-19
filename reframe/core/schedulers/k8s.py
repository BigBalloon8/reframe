import errno
import os
import signal
import socket
import time
from threading import Thread
import random
import warnings
import copy

from kubernetes import client, config
from kubernetes.client.rest import ApiException

import reframe.core.schedulers as sched
import reframe.utility.osext as osext
import reframe.utility.k8s_utils as k8s_utils
from reframe.core.backends import register_scheduler
from reframe.core.exceptions import JobError

class _K8Job(sched.Job):
    pod_config= variable(str, type(None), dict, value=None)
    namespace = variable(str, type(None), value=None)
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pod_name = None
        self._cancel_time = None
        self._log_thread:Thread = None
        self._job_kind = None
        self._num_pods = None

    @property
    def proc(self):
        return self._proc

    @property
    def f_stdout(self):
        return self._f_stdout

    @property
    def f_stderr(self):
        return self._f_stderr

    @property
    def cancel_time(self):
        return self._cancel_time


@register_scheduler('k8s')
class LocalJobScheduler(sched.JobScheduler):
    CANCEL_GRACE_PERIOD = 2
    WAIT_POLL_SECS = 0.001

    def make_job(self, *args, **kwargs):
        return _K8Job(*args, **kwargs)

    def submit(self, job: _K8Job):
        stdout = os.path.join(job.workdir, job.stdout)
        open(job.stderr, 'w+').close()
        open(job.stdout, 'w+').close()

        # Launch K8s launch pod
        pod_name, namespace, log_thread, num_pods, job_kind = k8s_utils.launch_k8s(job.namespace, copy.deepcopy(job.pod_config), stdout)

        # Update job info
        job._pod_name = pod_name
        job.namespace = namespace
        job._log_thread = log_thread
        job._job_kind = job_kind
        job._num_pods = num_pods
        job._jobid = random.randint(0, 1000000)
        job._state = 'RUNNING'
        job._submit_time = time.time()

    def emit_preamble(self, job):
        return []

    def allnodes(self):
        return [_LocalNode(socket.gethostname())]

    def filternodes(self, job, nodes):
        return [_LocalNode(socket.gethostname())]

    def _kill_pod(self, job: _K8Job):
        '''Deletes the kubernetes pod and stops the logging thread'''
        job._log_thread.join()
        if job._job_kind == 'job':
            k8s_utils._delete_job(job._pod_name, job.namespace)
        elif job._job_kind == 'pod':
            k8s_utils._delete_pod(job._pod_name, job.namespace)
        return

    def cancel(self, job: _K8Job):
        '''Deletes the kubernetes pod and stops the logging thread'''
        self._kill_pod(job)
        job._cancel_time = time.time()

    def wait(self, job):
        '''Wait for the spawned job to finish.

        As soon as the parent job process finishes, all of its spawned
        subprocesses will be forced to finish, too.

        Upon return, the whole process tree of the spawned job process will be
        cleared, unless any of them has called `setsid()`.
        '''
        while not self.finished(job):
            self.poll(job)
            time.sleep(self.WAIT_POLL_SECS)
        self._kill_pod(job)

    def finished(self, job: _K8Job):
        '''Query the k8s pod to check if its still alive'''
        if job.exception:
            raise job.exception
        
        if job._job_kind == 'job':
            return k8s_utils._has_job_finished(job._pod_name, job.namespace, job._num_pods)
        elif job._job_kind == 'pod':
            return k8s_utils._has_pod_finished(job._pod_name, job.namespace)


    def poll(self, *jobs):
        for job in jobs:
            self._poll_job(job)

    def _poll_job(self, job: _K8Job):
        if job is None:
            return

        if job.cancel_time:
            # Job has been cancelled; give it a grace period and kill it
            self.log(f'Job {job.jobid} has been cancelled;'
                     f'giving it a grace period')
            t_rem = self.CANCEL_GRACE_PERIOD - (time.time() - job.cancel_time)
            if t_rem > 0:
                time.sleep(t_rem)
            self._kill_pod(job)
            return

        # Job has not finished; check if we have reached a timeout
        if not self.finished(job):
            t_elapsed = time.time() - job.submit_time
            if job.time_limit and t_elapsed > job.time_limit:
                self._kill_pod(job)
                job._state = 'TIMEOUT'
                job._exception = JobError(
                    f'job timed out ({t_elapsed:.6f}s > {job.time_limit}s)',
                    job.jobid
                )
            return

class _LocalNode(sched.Node):
    def __init__(self, name):
        self._name = name

    @property
    def name(self):
        return self._name

    def in_state(self, state):
        return state.casefold() == 'idle'