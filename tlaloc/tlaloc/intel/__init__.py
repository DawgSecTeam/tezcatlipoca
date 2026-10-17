"""Intel selection: nakon when the artifacts are staged, blind otherwise.

The loader is the only place that decides which source the run uses, so
"blind fallback" is one branch rather than a property every consumer has to
re-derive (v1 decided it in the CLI, the digest and two tactics).
"""

from pathlib import Path

from .base import Intel
from .blind import BlindIntel
from .nakon import NakonIntel

# Presence of ANY of these means a competition was staged on this host.
NAKON_MARKERS = ("nakon-config.json", "box_vulns.json", ".deploy_state.json",
                 "box_services.json")


def load_intel(comp_dir, force_blind=False):
    """Intel for `comp_dir`; BlindIntel when nothing was staged (or forced)."""
    if force_blind:
        return BlindIntel(str(comp_dir))
    directory = Path(comp_dir or ".")
    if any((directory / name).exists() for name in NAKON_MARKERS):
        return NakonIntel(str(directory))
    return BlindIntel(str(directory))


__all__ = ["Intel", "BlindIntel", "NakonIntel", "load_intel", "NAKON_MARKERS"]
