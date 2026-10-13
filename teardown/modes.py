"""Plan builders: the two named kinds of teardown, assembled from steps.

build_terminal_plan is what destroy-competition.py (and, in-process, the scrim
harness) runs: gates -> portal -> artifacts -> red01 -> pre-stop -> terraform
-> templates -> remote access. build_reclaim_plan (phase 1) lives with the
deploy pipeline's migration."""

from teardown.plan import TERMINAL_TEARDOWN, TeardownPlan
from teardown.steps import (ArtifactStep, PortalStep, PreStopStep, RedStep,  # noqa: F401
                            RemoteAccessStep, TemplatesStep, TerraformStep)


def frozen_gate(comp_dir, frozen, full, end_of_competition):
    from teardown.machinery import refuse_frozen_full_teardown

    def _gate(ctx):
        del ctx
        refuse_frozen_full_teardown(comp_dir, frozen, full, end_of_competition)
    return _gate


# destroying against an empty state 'succeeds' and removes nothing
def tfstate_gate():
    from teardown.machinery import require_terraform_state

    def _gate(ctx):
        require_terraform_state(ctx.comp_dir, ctx.deployed_state)
    return _gate


# the run-id anchor (load_ownership) is not a gate here: the shim must load it to build the context, and the engine refuses before any mutation when it is missing
def build_terminal_plan(comp_dir, frozen, *, full, end_of_competition):
    gates = [frozen_gate(comp_dir, frozen, full, end_of_competition), tfstate_gate()]
    steps = [PortalStep(), ArtifactStep(), RedStep(), PreStopStep(),
             TerraformStep(), TemplatesStep(), RemoteAccessStep()]
    return TeardownPlan(TERMINAL_TEARDOWN, gates, steps)
