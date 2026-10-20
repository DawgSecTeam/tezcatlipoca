"""Where competition definitions live: the comps root and per-CLI comp-dir resolution."""

import subprocess
from pathlib import Path
from estate import config

REPO_ROOT = Path(__file__).resolve().parents[1]


def _main_worktree():
    # A practice worktree's parent is another worktree's, not the main checkout's, so the
    # comps sibling resolves through git's common dir (a linked worktree's .git pointer).
    try:
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=REPO_ROOT,
                                capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    path = Path(common)
    return (path if path.is_absolute() else REPO_ROOT / path).resolve().parent


def comps_root():
    env = config.active().knobs.competitions_root
    if env:
        return Path(env)
    for candidate in (Path("competitions"), REPO_ROOT / "competitions"):
        if candidate.is_dir():
            return candidate
    for base in (REPO_ROOT, _main_worktree()):
        if base and (base.parent / "tezcatlipoca-competitions").is_dir():
            return base.parent / "tezcatlipoca-competitions"
    return REPO_ROOT / "competitions"


def resolve_comp_dir(name=None, comp_dir=None):
    if comp_dir:
        return Path(comp_dir)
    return comps_root() / (name or "")
