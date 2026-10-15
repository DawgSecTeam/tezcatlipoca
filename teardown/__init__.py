"""The teardown object model (docs/internals.md#teardown).

One ownership proof (vm_ownership.Ownership), one refusal vocabulary
(Refusal), one classified estate view (Inventory), plans of declared steps
validated at construction, and one engine that applies policies, journals
completion, and owns the exit contract (exit 0 == verify_clean said clean).
The CLI scripts are thin shims over this package."""

from teardown.engine import CleanReport, TeardownEngine  # noqa: F401
from teardown.inventory import Inventory  # noqa: F401
from teardown.machinery import (TEARDOWN_WORKERS, load_ownership,  # noqa: F401
                                pre_stop_windows_boxes, refuse_frozen_full_teardown,
                                sweep_tagged_leftovers, teardown_templates)
from teardown.plan import PlanError, TeardownPlan  # noqa: F401
from teardown.refusal import Refusal  # noqa: F401
from teardown.steps import (EVIDENCE, REFUSE, TEARDOWN, WARN_AND_PROCEED,  # noqa: F401
                            ArtifactStep, EstateStep, PreStopStep, RedStep,
                            RemoteAccessStep, Step, StepResult, TeardownContext)
from infra.ownership import OWNED, MARKER, MISSING, UNPROVEN, Ownership  # noqa: F401

__all__ = [
    "CleanReport",
    "TeardownEngine",
    "Inventory",
    "Ownership",
    "PlanError",
    "Refusal",
    "Step",
    "StepResult",
    "TeardownContext",
    "TeardownPlan",
    # step vocabulary
    "EVIDENCE",
    "TEARDOWN",
    "REFUSE",
    "WARN_AND_PROCEED",
    # verdicts
    "OWNED",
    "MARKER",
    "MISSING",
    "UNPROVEN",
    # machinery (the moved, incident-hardened mechanics)
    "TEARDOWN_WORKERS",
    "load_ownership",
    "pre_stop_windows_boxes",
    "refuse_frozen_full_teardown",
    "sweep_tagged_leftovers",
    "teardown_templates",
    # steps
    "ArtifactStep",
    "EstateStep",
    "PreStopStep",
    "RedStep",
    "RemoteAccessStep",
]
