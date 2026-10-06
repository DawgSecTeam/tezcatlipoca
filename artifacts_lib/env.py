"""Host facts: timestamps, git revision, worktree detection."""

import subprocess
import time
from pathlib import Path

from .constants import REPO


def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S",
                         time.localtime(ts if ts is not None else time.time()))


def git_facts():
    """(rev, dirty) for this tree. Never raises: a report must not fail on git."""
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                             capture_output=True, text=True, timeout=10).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                                capture_output=True, text=True, timeout=20).stdout
        return rev, bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        return "", None


def in_worktree():
    """True when REPO is a linked worktree rather than the main checkout.

    Practice runs must happen in a throwaway worktree (AGENTS.md), and the competition dir — with
    every artifact in it — is per-worktree. The collector uses this to decide whether the
    finished test folder needs archiving somewhere durable before the worktree is removed."""
    try:
        git_dir = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=REPO,
                                 capture_output=True, text=True, timeout=10).stdout.strip()
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=REPO,
                                capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    if not git_dir or not common:
        return False
    return Path(REPO, git_dir).resolve() != Path(REPO, common).resolve()
