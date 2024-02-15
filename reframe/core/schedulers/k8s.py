import errno
import os
import signal
import socket
import time
from threading import Thread
import random
import warnings

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
        self.pod_config_changes = None
        self._cancel_time = None
        self._log_thread:Thread = None

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
    run_complete = False

    def make_job(self, *args, **kwargs):
        return _K8Job(*args, **kwargs)

    def submit(self, job: _K8Job):
        # Run from the absolute path
        stdout = os.path.join(job.workdir, job.stdout)
        f_stderr = open(job.stderr, 'w+').close()
        
        # The new process starts also a new session (session leader), so that
        # we can later kill any other processes that this might spawn by just
        # killing this one.

        pod_name, namespace, log_thread = k8s_utils.launch_pod(job.namespace, job.pod_config, stdout)

        # Update job info
        job._pod_name = pod_name
        job.namespace = namespace
        job._log_thread = log_thread
        job._jobid = random.randint(0, 1000000)
        job._state = 'RUNNING'
        job._submit_time = time.time()

    def emit_preamble(self, job):
        return []

    def allnodes(self):
        return [_LocalNode(socket.gethostname())]

    def filternodes(self, job, nodes):
        return [_LocalNode(socket.gethostname())]

    def _kill_all(self, job: _K8Job):
        '''Send SIGKILL to all the processes of the spawned job.'''
        k8s_utils.delete_pod(job._pod_name, job.namespace)
        job._log_thread.join()
        return
        try:
            os.killpg(job.jobid, signal.SIGKILL)
            job._signal = signal.SIGKILL
        except (ProcessLookupError, PermissionError):
            # The process group may already be dead or assigned to a different
            # group, so ignore this error
            self.log(f'pid {job.jobid} already dead')
        finally:
            # Close file handles
            job.f_stdout.close()
            job.f_stderr.close()
            job._state = 'FAILURE'

    def cancel(self, job: _K8Job):
        '''Cancel job.

        The SIGTERM signal will be sent first to all the processes of this job
        and after a grace period (default 2s) the SIGKILL signal will be send.

        This function waits for the spawned process tree to finish.
        '''
        k8s_utils.delete_pod(job._pod_name, job.namespace)
        job._log_thread.join()
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

    def finished(self, job: _K8Job):
        '''Check if the spawned process has finished.

        This function does not wait the process. It just queries its state. If
        the process has finished, you *must* call wait() to properly cleanup
        after it.
        '''
        if job.exception:
            raise job.exception

        if self.run_complete:
            return True

        return k8s_utils.has_finished(job._pod_name, job.namespace)

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
            self._kill_all(job)
            return

        # Job has not finished; check if we have reached a timeout
        if not self.finished(job):
            t_elapsed = time.time() - job.submit_time
            if job.time_limit and t_elapsed > job.time_limit:
                self._kill_all(job)
                job._state = 'TIMEOUT'
                job._exception = JobError(
                    f'job timed out ({t_elapsed:.6f}s > {job.time_limit}s)',
                    job.jobid
                )
            return
        
        self.run_complete = True
        self._kill_all(job)

class _LocalNode(sched.Node):
    def __init__(self, name):
        self._name = name

    @property
    def name(self):
        return self._name

    def in_state(self, state):
        return state.casefold() == 'idle'