"""test.json: identity, intent, phases and recorded source paths."""

import json
from pathlib import Path

from config.config_ops import write_state

from .constants import EVIDENCE_DIRS, MANIFEST_NAME, REPO, SCHEMA
from .env import git_facts, iso
from .paths import comp_name, run_id_from_state, test_dir, test_key


def load_manifest(test_path):
    try:
        data = json.loads((Path(test_path) / MANIFEST_NAME).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_manifest(test_path, manifest):
    manifest["schema"] = SCHEMA
    manifest["updated_at"] = iso()
    write_state(Path(test_path) / MANIFEST_NAME, manifest)
    return Path(test_path) / MANIFEST_NAME


def ensure_test(comp_dir, *, kind, run_id=None, key=None, script=None, label=None,
                teams=None, boxes=None, endpoint=None, node=None, nodes=None, extra=None):
    # Idempotent: existing truthy values win, and a folder held by another run id is
    # refused — that is the guard against two worktrees sharing one folder.
    comp_dir = Path(comp_dir)
    key = key or test_key(comp_dir, run_id)
    run_id = run_id if run_id is not None else run_id_from_state(comp_dir)
    path = test_dir(comp_dir, key)
    (path / "evidence").mkdir(parents=True, exist_ok=True)
    for sub in EVIDENCE_DIRS:
        (path / "evidence" / sub).mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(path)
    if manifest.get("key") and manifest["key"] != key:
        raise RuntimeError(f"test folder {path} holds key {manifest['key']!r}, not {key!r}")
    if manifest.get("run_id") and run_id and manifest["run_id"] != run_id:
        raise RuntimeError(
            f"test folder {path} belongs to {manifest['run_id']}, not {run_id} — refusing to "
            "mix two runs' artifacts in one folder")

    if not manifest:
        rev, dirty = git_facts()
        manifest = {
            "schema": SCHEMA,
            "comp": comp_dir.name,
            "comp_name": comp_name(comp_dir),
            "comp_dir": str(comp_dir),
            "key": key,
            "run_id": run_id or None,
            "kind": kind,
            "label": label or kind,
            "created_at": iso(),
            "created_by": {"script": script, "git_rev": rev, "worktree": str(REPO),
                           "dirty": dirty},
            "endpoint": endpoint,
            "node": node,
            "nodes": nodes or ([node] if node else []),
            "teams": teams,
            "boxes": list(boxes or []),
            "agents": {"red": {"present": False}, "blue": {"present": False}},
            "paths": {},
            "event": {"t0": None, "duration_min": None},
            "verify": {},
            "verdict": {},
            "writeup": {"status": "needs-writeup", "author": None, "completed_at": None},
            "teardown": {},
            "phases": [],
        }
    else:
        for field, value in (("kind", kind), ("label", label), ("teams", teams),
                             ("endpoint", endpoint), ("node", node)):
            if value is not None and not manifest.get(field):
                manifest[field] = value
        if boxes:
            manifest["boxes"] = list(boxes)
        if nodes:
            manifest["nodes"] = sorted(set(manifest.get("nodes") or []) | set(nodes))
    for field, value in (extra or {}).items():
        if value is not None:
            manifest[field] = value
    save_manifest(path, manifest)
    return path, manifest


def record_phase(test_path, phase, *, t0=None, **fields):
    # Mirrors the harness's run.json `phase`: both the `phases` list and the scalar are written.
    manifest = load_manifest(test_path)
    manifest.setdefault("phases", []).append({"phase": phase, "at": iso(), "t0": t0})
    manifest["phase"] = phase
    for field, value in fields.items():
        manifest[field] = value
    save_manifest(test_path, manifest)
    return manifest


def update_manifest(test_path, **fields):
    manifest = load_manifest(test_path)
    for field, value in fields.items():
        if value is not None:
            manifest[field] = value
    save_manifest(test_path, manifest)
    return manifest


def _absolute(value):
    # Stored absolute: the manifest outlives the writer's process and is read from a
    # different CWD (teardown from the repo root, harness from its worktree root).
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [str(Path(item).resolve()) for item in value]
    return str(Path(value).resolve())


def record_paths(test_path, **paths):
    # Merged, not replaced: the harness knows the run dir and blue workdirs, teardown
    # learns the engine capture location; neither may erase the other.
    manifest = load_manifest(test_path)
    known = manifest.setdefault("paths", {})
    for field, value in paths.items():
        if value is not None:
            known[field] = _absolute(value)
    save_manifest(test_path, manifest)
    return known


def resolve_recorded(value, comp_dir):
    # Older/hand-written manifests can hold relative paths: try CWD, then comp_dir.
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    for base in (Path.cwd(), Path(comp_dir or ".")):
        candidate = base / path
        if candidate.exists():
            return candidate
    return path
