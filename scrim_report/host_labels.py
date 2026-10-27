import json
from pathlib import Path

from common.comp_paths import comps_root


REPO = Path(__file__).resolve().parent.parent


# {} when unresolvable: the raw octet beats inventing a name from another lineup
def _boxes_candidates(run_dir):
    """Every place this run's boxes.json may live, best guess first."""
    run_dir = Path(run_dir).resolve()
    name = run_dir.name
    comps = comps_root()
    cands = []
    if ".automated-tests" in run_dir.parts:
        i = run_dir.parts.index(".automated-tests")
        cands.append(Path(*run_dir.parts[:i]) / "boxes.json")
    cands.append(comps / name / "boxes.json")
    if comps.is_dir():
        # run dirs are often suffixed ("comp-run2"): longest matching comp name wins
        prefixes = sorted((p for p in comps.iterdir()
                           if p.is_dir() and name.startswith(p.name)),
                          key=lambda p: len(p.name), reverse=True)
        cands += [p / "boxes.json" for p in prefixes]
    return cands


def box_labels(run_dir):
    for cand in _boxes_candidates(run_dir):
        try:
            boxes = json.loads(cand.read_text())
        except (OSError, ValueError):
            continue
        labels = {str(b["last_octet"]): b["name"] for b in boxes
                  if isinstance(b, dict) and "last_octet" in b and "name" in b}
        if labels:
            return labels
    return {}


# unresolvable -> no Windows octets: the foothold gate counts none rather than guessing slots
def windows_octets(run_dir):
    for cand in _boxes_candidates(run_dir):
        try:
            boxes = json.loads(cand.read_text())
        except (OSError, ValueError):
            continue
        octets = {str(b["last_octet"]) for b in boxes
                  if isinstance(b, dict) and "last_octet" in b
                  and "windows" in str(b.get("template", "")).lower()}
        if octets:
            return octets
    return set()


TEAM_IDENTIFIERS = {}


def load_teams(run_dir):
    """The comp's team keys -> identifiers, so a label never guesses `octet - 100`.

    Returns the mapping for the caller to pass to `host_label`; `{}` when unresolvable.
    """
    run_dir = Path(run_dir).resolve()
    for cand in _boxes_candidates(run_dir):
        teams_path = cand.parent / "teams.json"
        try:
            teams = json.loads(teams_path.read_text())
        except (OSError, ValueError):
            continue
        mapping = {str(v.get("identifier")): k for k, v in (teams or {}).items()
                   if isinstance(v, dict) and v.get("identifier") is not None}
        if mapping:
            return mapping
    return {}


def host_label(ip, labels=None, teams=None):
    if not ip:
        return "?"
    octets = ip.split(".")
    host = (labels or {}).get(octets[-1], f".{octets[-1]}" if len(octets) == 4 else ip)
    team, name = "", host
    if len(octets) == 4 and octets[2].isdigit():
        mapping = teams if teams is not None else TEAM_IDENTIFIERS
        key = mapping.get(octets[2])
        if key:
            team, name = str(key).removeprefix("team"), host
        elif 100 < int(octets[2]) < 200:
            # No teams.json to consult (an archived run): the 101+i convention beats a bare
            # octet. Real identifiers always win when the lineup is there.
            team = str(int(octets[2]) - 100)
    return f"{team}:{name}" if team else host
