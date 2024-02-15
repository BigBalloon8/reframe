from reframe.core.backends import register_launcher
from reframe.core.launchers import JobLauncher

@register_launcher('k8s', local=True)
class K8sLauncher(JobLauncher):
    """The k8s launcher doesnt do anything as the jobs are launched by the scheduler"""
    def command(self, job):
        return [""]