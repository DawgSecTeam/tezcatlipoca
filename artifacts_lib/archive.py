"""Durable copies of test folders (outside the repo, hash-verified)."""

import os
import shutil
from pathlib import Path

from utils import record_degradation

from .hashing import sha256_file
from .paths import test_dir


def default_archive_root():
    """Durable home for test folders whose worktree is about to be deleted.

    Same convention as the deploy locks (~/.tezcatlipoca/): outside the repo, so discarding a
    practice-run worktree cannot take a month of test history with it."""
    return Path(os.environ.get("TEZ_ARTIFACTS_ARCHIVE")
                or Path.home() / ".tezcatlipoca" / "automated-tests")


def archive_test(comp_dir, key, *, dest_root=None):
    """Copy a finished test folder outside the repo and verify every byte landed.

    Only needed when the comp dir lives in a linked worktree — but there the alternative is
    losing the artifact with the worktree, so the copy is verified by hash, not assumed."""
    comp_dir = Path(comp_dir)
    source = test_dir(comp_dir, key)
    if not source.is_dir():
        raise RuntimeError(f"no such test folder: {source}")
    dest = Path(dest_root or default_archive_root()) / comp_dir.name / key
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest)
    drift = [str(path.relative_to(source)) for path in source.rglob("*") if path.is_file()
             and (not (dest / path.relative_to(source)).exists()
                  or sha256_file(dest / path.relative_to(source)) != sha256_file(path))]
    if drift:
        raise RuntimeError(f"archive verification failed for {len(drift)} file(s): {drift[:5]}")
    record_degradation("artifacts-archived", f"{source} -> {dest}")
    return dest
