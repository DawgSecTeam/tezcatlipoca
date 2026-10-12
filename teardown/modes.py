"""Plan builders: the two named kinds of teardown, assembled from steps.

build_terminal_plan is what destroy-competition.py (and, in-process, the scrim
harness) runs: gates -> portal -> artifacts -> red01 -> pre-stop -> terraform
-> templates -> remote access. build_reclaim_plan (phase 1) lives with the
deploy pipeline's migration."""

from teardown.plan import RECLAIM, TERMINAL_TEARDOWN, TeardownPlan
from teardown.steps import (ArtifactStep, PortalStep, PreStopStep, RedStep,  # noqa: F401
                            RemoteAccessStep, TemplatesStep, TerraformStep)


def frozen_gate(comp_dir, frozen, full, end_of_competition):
    from teardown.machinery import refuse_frozen_full_teardown

    def _gate(ctx):
        del ctx
        refuse_frozen_full_teardown(comp_dir, frozen, full, end_of_competition)
    return _gate


def tfstate_gate():
    """Refuse a deployed comp whose per-comp terraform state is missing —
    destroying against an empty state 'succeeds' and removes nothing."""
    from teardown.machinery import require_terraform_state

    def _gate(ctx):
        require_terraform_state(ctx.comp_dir, ctx.deployed_state)
    return _gate


def build_terminal_plan(comp_dir, frozen, *, full, end_of_competition):
    """The end-of-range teardown. Step order is the guarded order; the plan
    validates evidence-before-destruction at construction. The run-id anchor
    (load_ownership) is not a gate here: the shim must load it to build the
    context, and the engine refuses before any mutation when it is missing."""
    gates = [frozen_gate(comp_dir, frozen, full, end_of_competition), tfstate_gate()]
    steps = [PortalStep(), ArtifactStep(), RedStep(), PreStopStep(),
             TerraformStep(), TemplatesStep(), RemoteAccessStep()]
    return TeardownPlan(TERMINAL_TEARDOWN, gates, steps)


def build_reclaim_plan(steps):
    """Phase 1's reclaim of the prior run (RECLAIM kind: no evidence steps —
    the prior run's evidence was that run's teardown's job)."""
    return TeardownPlan(RECLAIM, [], steps)
