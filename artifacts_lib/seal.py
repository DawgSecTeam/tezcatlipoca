"""Verify a folder against its collection record; seal a finished write-up."""

import os
from pathlib import Path

from config_ops import write_state

from .archive import archive_test
from .collect import load_collection
from .constants import COLLECTION_NAME, MANIFEST_NAME, OK, REPORT_NAME, SEALED, TODO_MARK
from .env import in_worktree, iso
from .hashing import sha256_file
from .index import update_index
from .manifest import load_manifest, save_manifest


def verify_test(test_path):
    """Re-hash everything the collection claims, and report drift.

    Reports are evidence, so they get the same treatment as any other artifact: a hash that is
    checked, not a filename that is trusted."""
    test_path = Path(test_path)
    manifest = load_manifest(test_path)
    collection = load_collection(test_path)
    problems, checked = [], 0
    for target in collection.get("targets", []):
        for item in target.get("files", []):
            if item.get("status") not in (OK, SEALED):
                continue
            path = test_path / item.get("local", "")
            if not path.exists():
                problems.append(f"missing: {item.get('local')}")
                continue
            checked += 1
            if item.get("sha256") and sha256_file(path) != item["sha256"]:
                problems.append(f"hash drift: {item.get('local')}")
    for derived in collection.get("derived", []):
        path = test_path / derived["path"]
        if not path.exists():
            problems.append(f"missing canonical document: {derived['path']}")
            continue
        checked += 1
        if derived.get("sha256") and sha256_file(path) != derived["sha256"]:
            problems.append(f"canonical document changed after collection: {derived['path']}")
    for name in (MANIFEST_NAME, COLLECTION_NAME, REPORT_NAME):
        if not (test_path / name).exists():
            problems.append(f"missing {name}")
    todos = 0
    report = test_path / REPORT_NAME
    if report.exists():
        todos = report.read_text().count(TODO_MARK)
    return {"ok": not problems and todos == 0, "checked": checked, "problems": problems,
            "todos": todos, "writeup": (manifest.get("writeup") or {}).get("status")}


def seal_test(test_path, *, author=None, force=False):
    """Flip the write-up to done: refuse while judgement markers remain, then re-hash.

    The seal is what makes `writeup.status` mean something. Without it, "done" is a claim nobody
    checked — and the recommendations section is the part of this feature most likely to be
    skipped."""
    test_path = Path(test_path)
    manifest = load_manifest(test_path)
    report = test_path / REPORT_NAME
    if not report.exists():
        raise RuntimeError(f"no {REPORT_NAME} in {test_path} — nothing to seal")
    unfilled = report.read_text().count(TODO_MARK)
    if unfilled and not force:
        raise RuntimeError(
            f"{REPORT_NAME} still has {unfilled} unfilled section(s) marked TODO(author) — "
            "fill them, or seal with force if a section genuinely does not apply")
    manifest.setdefault("writeup", {})
    manifest["writeup"].update({"status": "done",
                                "author": author or os.environ.get("USER"),
                                "completed_at": iso()})
    save_manifest(test_path, manifest)
    collection = load_collection(test_path)
    for derived in collection.get("derived", []):
        path = test_path / derived["path"]
        if path.exists():
            derived["sha256_after_writeup"] = sha256_file(path)
    if collection:
        write_state(test_path / COLLECTION_NAME, collection)
    comp_dir = Path(manifest.get("comp_dir") or test_path.parent.parent)
    update_index(comp_dir)
    # A finished write-up in a throwaway worktree is exactly what the teardown-time archive
    # holds only as a skeleton — refresh it now, or the archive keeps a blank REPORT.md.
    archived = None
    if in_worktree():
        try:
            archived = str(archive_test(comp_dir, manifest.get("key") or test_path.name))
        except (OSError, RuntimeError):
            archived = None
    return {"sealed": str(test_path), "author": manifest["writeup"]["author"],
            "archived": archived, "verify": verify_test(test_path)}
