"""The teardown object model (docs/teardown-redesign.md).

One ownership proof (vm_ownership.Ownership), one refusal vocabulary
(Refusal), one classified estate view (Inventory), plans of declared steps
validated at construction, and one engine that applies policies, journals
completion, and owns the exit contract (exit 0 == verify_clean said clean).
The CLI scripts are thin shims over this package."""

from teardown.engine import TEARDOWN_JOURNAL, CleanReport, TeardownEngine  # noqa: F401
from teardown.inventory import Inventory  # noqa: F401
from teardown.machinery import (DESTROY_ATTEMPT_TIMEOUT_S, TEARDOWN_WORKERS,  # noqa: F401
                                clear_stale_state_lock, destroy_with_recovery,
                                load_ownership, pre_stop_windows_boxes,
                                refuse_frozen_full_teardown, require_terraform_state,
                                sweep_tagged_leftovers, teardown_templates)
from teardown.plan import (RECLAIM, TERMINAL_TEARDOWN, PlanError,  # noqa: F401
                           TeardownPlan)
from teardown.refusal import Refusal  # noqa: F401
from teardown.steps import (EVIDENCE, REFUSE, TEARDOWN, WARN_AND_PROCEED,  # noqa: F401
                            ArtifactStep, CallStep, PreStopStep, RedStep,
                            RemoteAccessStep, Step, StepResult, TemplatesStep,
                            TerraformStep, TeardownContext)
from vm_ownership import OWNED, MARKER, MISSING, UNPROVEN, Ownership  # noqa: F401

__all__ = [
    "CleanReport",
    "TEARDOWN_JOURNAL",
    "TeardownEngine",
    "Inventory",
    "Ownership",
    "PlanError",
    "Refusal",
    "Step",
    "StepResult",
    "TeardownContext",
    "TeardownPlan",
    # plan kinds
    "RECLAIM",
    "TERMINAL_TEARDOWN",
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
    "DESTROY_ATTEMPT_TIMEOUT_S",
    "TEARDOWN_WORKERS",
    "clear_stale_state_lock",
    "destroy_with_recovery",
    "load_ownership",
    "pre_stop_windows_boxes",
    "refuse_frozen_full_teardown",
    "require_terraform_state",
    "sweep_tagged_leftovers",
    "teardown_templates",
    # steps
    "ArtifactStep",
    "PreStopStep",
    "RedStep",
    "RemoteAccessStep",
    "TemplatesStep",
    "TerraformStep",
]
