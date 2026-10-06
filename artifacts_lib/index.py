"""The roll-up index.json: derived from the test folders on disk."""

import json
from pathlib import Path

from config_ops import write_state

from .collect import load_collection
from .constants import INDEX_NAME, MANIFEST_NAME, REPORT_NAME, SCHEMA
from .env import iso
from .manifest import load_manifest
from .paths import artifacts_root
from .status import warn_summary


def update_index(comp_dir):
    """Rewrite `<comp>/.automated-tests/index.json` from the test folders on disk.

    Derived, never authoritative: it can always be rebuilt, so a stale index is a cosmetic
    problem rather than a data-loss one."""
    root = artifacts_root(comp_dir)
    rows = []
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if not (path / MANIFEST_NAME).exists():
                continue
            manifest = load_manifest(path)
            collection = load_collection(path)
            rows.append({
                "key": manifest.get("key") or path.name,
                "run_id": manifest.get("run_id"),
                "kind": manifest.get("kind"),
                "label": manifest.get("label"),
                "created_at": manifest.get("created_at"),
                "phase": manifest.get("phase"),
                "verdict": (manifest.get("verdict") or {}).get("status"),
                "agents": {side: bool((manifest.get("agents") or {}).get(side, {}).get("present"))
                           for side in ("red", "blue")},
                "writeup": (manifest.get("writeup") or {}).get("status"),
                "report": (path / REPORT_NAME).exists(),
                "warnings": len(warn_summary(collection, manifest, path)) if collection else 0,
            })
    rows.sort(key=lambda row: (row.get("created_at") or "", row["key"]), reverse=True)
    index = {"schema": SCHEMA, "comp": Path(comp_dir).name, "updated_at": iso(), "tests": rows}
    root.mkdir(parents=True, exist_ok=True)
    write_state(root / INDEX_NAME, index)
    return root / INDEX_NAME


def list_tests(comp_dir):
    """Index rows, rebuilding the index when it is missing or unreadable."""
    index = artifacts_root(comp_dir) / INDEX_NAME
    if not index.exists():
        update_index(comp_dir)
    try:
        return json.loads(index.read_text()).get("tests", [])
    except (OSError, ValueError):
        update_index(comp_dir)
        return json.loads(index.read_text()).get("tests", [])
