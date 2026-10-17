"""Plan builders: the named kinds of teardown, assembled from steps."""

from teardown.plan import TeardownPlan
from teardown.steps import (ArtifactStep, EngineCaptureStep, EstateStep,  # noqa: F401
                            PortalStep, PreStopStep, RedStep, RemoteAccessStep)


# the frozen and run-id refusals fire in teardown.runner before this plan is built
def build_terminal_plan():
    steps = [PortalStep(), EngineCaptureStep(), ArtifactStep(), RedStep(), PreStopStep(),
             EstateStep(), RemoteAccessStep()]
    return TeardownPlan([], steps)
