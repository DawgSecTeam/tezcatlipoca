"""Plan builders: the two named kinds of teardown, assembled from steps.

build_terminal_plan is what destroy-competition.py (and, in-process, the scrim
harness) runs: portal -> artifacts -> red01 -> pre-stop -> terraform
-> templates -> remote access. It is the only plan kind: deploy phase 1 no
longer destroys a range, so nothing else composes one."""

from teardown.plan import TeardownPlan
from teardown.steps import (ArtifactStep, PortalStep, PreStopStep, RedStep,  # noqa: F401
                            RemoteAccessStep, TemplatesStep, TerraformStep)


# the frozen, run-id and tfstate refusals fire in teardown.runner before this plan is built
def build_terminal_plan():
    steps = [PortalStep(), ArtifactStep(), RedStep(), PreStopStep(),
             TerraformStep(), TemplatesStep(), RemoteAccessStep()]
    return TeardownPlan([], steps)
