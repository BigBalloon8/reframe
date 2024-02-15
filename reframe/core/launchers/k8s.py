from reframe.core.backends import register_launcher
from reframe.core.launchers import JobLauncher

@register_launcher('k8s', local=True)
class K8sLauncher(JobLauncher):
    def command(self, job):
        return [""]