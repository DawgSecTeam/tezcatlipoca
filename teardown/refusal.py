"""The one refusal vocabulary. Defined in vm_ownership (next to the verdicts that
produce it, so the ownership proof has zero imports); re-exported here because
this is the name every teardown module and test refers to."""

from vm_ownership import Refusal  # noqa: F401

__all__ = ["Refusal"]
