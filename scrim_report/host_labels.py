import json
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def box_labels(run_dir):
    """octet(str) -> box name, resolved from the competition's own boxes.json.

    The fixed HOST_BY_OCTET table hardcoded the 17b lineup, so every red timeline entry
    and every target label was WRONG on any other competition (cde-2026 is
    ad01/ftp01/web01/db01, amongus-cde-2026 is mira/skeld/airship/polus) — even though
    the report already knew the competition directory through the run dir's name
    (audit find D8). Returns {} when it cannot resolve one, so host_label degrades to the
    raw dotted octet instead of inventing a name from the wrong lineup.
    """
    name = Path(run_dir).resolve().name
    comps = REPO / "competitions"
    cands = [comps / name / "boxes.json"]
    # Run dirs live INSIDE their competition now (competitions/<comp>/.automated-tests/
    # <run-id>/): the competition's own boxes.json is three levels up. Miss this and a
    # fully-labeled comp degrades to raw addresses.
    p = Path(run_dir).resolve()
    if ".automated-tests" in p.parts:
        i = p.parts.index(".automated-tests")
        cands.append(Path(*p.parts[:i]) / "boxes.json")
    if comps.is_dir():
        # run dirs are often suffixed (cde-2026-run2): take the longest competition
        # name that prefixes the run dir.
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


def windows_octets(run_dir):
    """The last octets that are WINDOWS boxes, from the competition's boxes.json.

    The windows_footholds fallback used to hardcode octets 2/3 (the 17b dc01/win01
    slots); scrim-fresh-a's only Windows box sits at .5, so red's two real win01
    cred_spray footholds counted 0 and the gate failed red for footholds it held.
    Unresolvable -> no Windows octets (the gate counts none rather than guessing)."""
    p = Path(run_dir).resolve()
    cands = []
    if ".automated-tests" in p.parts:
        i = p.parts.index(".automated-tests")
        cands.append(Path(*p.parts[:i]) / "boxes.json")
    cands.append(REPO / "competitions" / p.name / "boxes.json")
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
