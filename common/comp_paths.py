"""Where competition definitions live: the comps root and per-CLI comp-dir resolution."""

from pathlib import Path
from estate import config

REPO_ROOT = Path(__file__).resolve().parents[1]


def comps_root():
    env = config.active().knobs.competitions_root
    if env:
        return Path(env)
    for candidate in (Path("competitions"), REPO_ROOT / "competitions"):
        if candidate.is_dir():
            return candidate
    sibling = REPO_ROOT.parent / "tezcatlipoca-competitions"
    return sibling if sibling.is_dir() else REPO_ROOT / "competitions"


def resolve_comp_dir(name=None, comp_dir=None):
    if comp_dir:
        return Path(comp_dir)
    return comps_root() / (name or "")
