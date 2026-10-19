import json
from pathlib import Path

from common.comp_paths import comps_root


REPO = Path(__file__).resolve().parent.parent


# {} when unresolvable: the raw octet beats inventing a name from another lineup
def box_labels(run_dir):
    name = Path(run_dir).resolve().name
    comps = comps_root()
    cands = [comps / name / "boxes.json"]
    # run dirs live inside the comp (competitions/<comp>/.automated-tests/<run-id>/): that comp's boxes.json is 3 up
    p = Path(run_dir).resolve()
    if ".automated-tests" in p.parts:
        i = p.parts.index(".automated-tests")
        cands.append(Path(*p.parts[:i]) / "boxes.json")
    if comps.is_dir():
        # run dirs are often suffixed ("comp-run2"): longest matching comp name wins
        prefixes = sorted((p for p in comps.iterdir()
                           if p.is_dir() and name.startswith(p.name)),
                          key=lambda p: len(p.name), reverse=True)
        cands += [p / "boxes.json" for p in prefixes]
    for cand in cands:
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
    p = Path(run_dir).resolve()
    cands = []
    if ".automated-tests" in p.parts:
        i = p.parts.index(".automated-tests")
        cands.append(Path(*p.parts[:i]) / "boxes.json")
    cands.append(comps_root() / p.name / "boxes.json")
    for cand in cands:
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


def host_label(ip, labels=None):
    if not ip:
        return "?"
    octets = ip.split(".")
    host = (labels or {}).get(octets[-1], f".{octets[-1]}" if len(octets) == 4 else ip)
    # team identifiers are 100+i (101 -> team1, ...); anything else isn't a team box
    team = ""
    if len(octets) == 4 and octets[2].isdigit() and 100 < int(octets[2]) < 200:
        team = str(int(octets[2]) - 100)
    return f"{team}:{host}" if team else host
