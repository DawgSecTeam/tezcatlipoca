"""Plan builders: the named kinds of teardown, assembled from steps.

build_terminal_plan is what destroy-competition.py (and, in-process, the scrim
harness) runs: portal -> artifacts -> red01 -> pre-stop -> estate
-> remote access. It is the only plan kind: deploy phase 1 no
longer destroys a range, so nothing else composes one."""

from teardown.plan import TeardownPlan
from teardown.steps import (ArtifactStep, EstateStep, PortalStep, PreStopStep,  # noqa: F401
                            RedStep, RemoteAccessStep)


# the frozen and run-id refusals fire in teardown.runner before this plan is built
def build_terminal_plan():
    steps = [PortalStep(), ArtifactStep(), RedStep(), PreStopStep(),
             EstateStep(), RemoteAccessStep()]
    return TeardownPlan([], steps)
