"""What state each canonical document is in, and the warnings that follow from it."""

import re
from pathlib import Path

from .constants import ABSENT, LOST, NOT_COLLECTED, SIDES


def document_state(test_path, filename, collection):
    # A stub is a file that exists saying "not available" — checking only for a
    # missing file misses every real gap.
    test_path = Path(test_path)
    path = test_path / filename
    for derived in collection.get("derived", []):
        if derived.get("path") == filename and path.exists():
            return "present"
    if not path.exists():
        return ABSENT
    match = re.search(r"^status:\s*(\S+)", path.read_text(errors="ignore"), re.M)
    return (match.group(1) if match else "present")


def warn_summary(collection, manifest, test_path=None):
    # Severity comes from the manifest: a run with no red agent is not missing a red report.
    # Nothing blocks — a dead guest box must never wedge a teardown.
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
    if test_path:
        folder = Path(test_path)
    else:
        comp_dir, key = manifest.get("comp_dir"), manifest.get("key")
        folder = Path(comp_dir) / ".automated-tests" / key if comp_dir and key else None
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
    # Never-attempted, attempted-and-lost, and no-such-side must not be conflated or the
    # folder lies; a wholesale-failed target's own status answers for the documents it carried.
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
