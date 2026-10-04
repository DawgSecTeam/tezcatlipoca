"""What state each canonical document is in, and the warnings that follow from it."""

import re
from pathlib import Path

from .constants import ABSENT, DIRNAME, LOST, NOT_COLLECTED, SIDES


def document_state(test_path, filename, collection):
    """What state a canonical document is actually in: `present`, or the stub's own status.

    The distinction matters because a stub is a file that exists and says "not available" —
    warning only on a missing *file* would either miss every real gap (the stub exists) or
    ignore the document on disk entirely."""
    test_path = Path(test_path)
    path = test_path / filename
    for derived in collection.get("derived", []):
        if derived.get("path") == filename and path.exists():
            return "present"
    if not path.exists():
        return ABSENT
    match = re.search(r"^status:\s*(\S+)", path.read_text(errors="ignore"), re.M)
    return (match.group(1) if match else "present")


def test_path_for(manifest, test_path=None):
    """The test folder for a manifest, when the caller did not already have it in hand."""
    if test_path:
        return Path(test_path)
    comp_dir, key = manifest.get("comp_dir"), manifest.get("key")
    return Path(comp_dir) / DIRNAME / key if comp_dir and key else None


def warn_summary(collection, manifest, test_path=None):
    """The lines teardown prints loudly when something that should be there is not.

    Severity comes from the manifest, not the collection: a run with no red agent is not missing
    anything when there is no red report. Nothing here blocks — the operator chose warn-and-
    proceed (2026-10-03) because a dead guest box must never wedge a teardown."""
    warnings = []
    for target in collection.get("targets", []):
        for item in target.get("files", []):
            if item.get("status") in LOST:
                warnings.append(f"{target['name']}: {item.get('want')} — {item['status']}"
                                + (f" ({item['reason']})" if item.get("reason") else ""))
        if target.get("status") in LOST and not target.get("errors"):
            warnings.append(f"{target['name']}: {target['status']}"
                            + (f" ({target.get('reason')})" if target.get("reason") else ""))
    agents = manifest.get("agents") or {}
    folder = test_path_for(manifest, test_path)
    for side, filename in SIDES.items():
        if not (agents.get(side) or {}).get("present"):
            continue
        state = document_state(folder, filename, collection) if folder else ABSENT
        if state != "present":
            warnings.append(f"{filename} is {state.upper()} although this run had a {side} "
                            "agent — that side's narrative is not in this folder")
    return warnings


def side_status(collection, side):
    filename = SIDES[side]
    for derived in collection.get("derived", []):
        if derived.get("path") == filename:
            return "present"
    for target in collection.get("targets", []):
        for item in target.get("files", []):
            if item.get("canonical") == filename and item.get("status") in LOST:
                return item["status"]
    return "absent"


def why_missing(collection, side):
    """(status, reason) for a canonical document we wanted and did not get.

    Three genuinely different situations, and conflating them is how an artifact folder starts
    lying: nothing was attempted (recoverable — the sources are still out there), an attempt was
    made and the source was empty, unreachable, or already destroyed, or the run never had that
    side at all. A target that failed wholesale records no per-file status, so its own status is
    the honest answer for the document it was meant to carry."""
    filename = SIDES[side]
    attempted = False
    for target in collection.get("targets", []):
        for item in target.get("files", []):
            if item.get("canonical") == filename:
                attempted = True
                if item.get("status") in LOST + (ABSENT,):
                    return item["status"], item.get("reason") or "not obtained"
        if (target.get("status") in LOST
                and filename in (target.get("canonical_documents") or [])):
            attempted = True
            reason = (target.get("reason") or "; ".join(target.get("errors") or [])
                      or f"{target.get('name')} is {target['status']}")
            return target["status"], reason
    if not attempted:
        return (NOT_COLLECTED,
                "the evidence this report would come from was never collected")
    return ABSENT, "no source produced it"
