"""Where competition definitions live: the comps root and per-CLI comp-dir resolution.

Competition dirs moved to their own repo (tezcatlipoca-competitions), outside the engine
checkout: the dashboard runs drivers against a per-run workspace copy via --comp-dir, and
manual runs point --comp-dir at a comps checkout (or name one under the comps root). Root
resolution: TEZ_COMPETITIONS_ROOT wins; else ./competitions (the old CWD-relative idiom,
run from a worktree root) or the checkout's own competitions/; else the sibling
../tezcatlipoca-competitions repo.
"""

from pathlib import Path
from estate import config

REPO_ROOT = Path(__file__).resolve().parents[1]


def comps_root():
    env = config.active().knobs.comps_root
    if env:
        return Path(env)
    for candidate in (Path("competitions"), REPO_ROOT / "competitions"):
        if candidate.is_dir():
            return candidate
    sibling = REPO_ROOT.parent / "tezcatlipoca-competitions"
    return sibling if sibling.is_dir() else REPO_ROOT / "competitions"


def resolve_comp_dir(name=None, comp_dir=None):
    # An explicit --comp-dir path wins; a bare NAME resolves under the comps root.
    if comp_dir:
        return Path(comp_dir)
    return comps_root() / (name or "")
