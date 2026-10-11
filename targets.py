"""(team, box) target abstraction: vmid arithmetic, enumeration, frozen targets.json."""

import json
from pathlib import Path

def vm_id_for(identifier, box_index):
    return 200 + int(identifier) * 10 + box_index


def box_index(boxes, box_name):
    """Canonical position of a box by name — matches terraform's index(names, name)."""
    for i, b in enumerate(boxes):
        if b["name"] == box_name:
            return i
    raise KeyError(f"box {box_name!r} not in boxes list")


def persist_targets(comp_dir, targets, boxes):
    """Freeze (team,box)->vmid at deploy so later tools don't recompute it from boxes.json order."""
    data = {
        "box_order": [b["name"] for b in boxes],
        "targets": {
            t["vm_name"]: {
                "team_key": t["team_key"],
                "identifier": t["identifier"],
                "box_name": t["box_name"],
                "vmid": t["vmid"],
                "ip": t["ip"],
                **({"node": t["node"]} if t.get("node") else {}),
            }
            for t in targets
        },
    }
    (Path(comp_dir) / "targets.json").write_text(json.dumps(data, indent=2))


def load_targets(comp_dir, teams, boxes):
    """Targets with vmid read from targets.json (frozen at deploy); recompute if absent.

    Refuses when boxes.json was reordered since deploy: vmid is positional, so a
    reorder would silently retarget a different VM (and terraform would diverge too)."""
    path = Path(comp_dir) / "targets.json"
    if not path.exists():
        print("  (targets.json absent — deriving vmids from boxes.json order; "
              "reorder-unsafe for ranges deployed before this was added)")
        return enumerate_targets(teams, boxes)
    data = json.loads(path.read_text())
    current = [b["name"] for b in boxes]
    frozen_order = data.get("box_order") or []
    # Append-only is safe: vmid is positional, so boxes ADDED after deploy leave every
    # earlier index (and therefore every frozen vmid) untouched — a Compfile can gain a
    # box later (scrim-one gained the unmanaged in_path fw01) and the range it describes
    # is still the same range. A reorder or a removal is what must refuse.
    if current[:len(frozen_order)] != frozen_order:
        raise SystemExit(
            f"  ERROR: boxes.json box order changed since deploy (was {frozen_order}, "
            f"now {current}). VM identity (vmid) is positional — restore the original order in "
            "boxes.json before redeploy/verify, or tear down and redeploy from scratch."
        )
    frozen = data.get("targets", {})
    result = []
    for t in enumerate_targets(teams, boxes):
        f = frozen.get(t["vm_name"])
        if f is not None:
            t = {**t, "vmid": f["vmid"], "ip": f.get("ip", t["ip"])}
        result.append(t)
    return result


def enumerate_targets(teams, boxes, placement=None, default_node=None):
    """One target per (team, box); build from full lists then filter — vmid is positional.

    With a multi-node placement, each target carries `node` + `slot` from its team's
    placement; without one, `default_node` (the env node) is stamped so downstream
    node-scoped calls have one uniform field."""
    team_node = placement.get("team_nodes") if placement else None
    team_slot = placement.get("team_slots") if placement else None

    def _pve_node(name):
        # placement keys (team_nodes, node record names) are nodes.json NAMES; every
        # /nodes/<name>/... API path needs the PVE hostname inside that record.
        rec = (placement or {}).get("nodes", {}).get(name)
        return rec["node"] if rec else name

    return [
        {
            "team_key": team_key,
            "identifier": str(team["identifier"]),
            "box": box,
            "box_name": box["name"],
            "box_idx": box_idx,
            "ip": f"192.168.{team['identifier']}.{box['last_octet']}",
            "vmid": vm_id_for(team["identifier"], box_idx),
            "vm_name": (f"{team_key}-{box['name']}" if team_key == "team1"
                        else f"{team['identifier']}-{box['name']}"),
            "machine": f"{box['name']}-team{team['identifier']}",
            "node": _pve_node((team_node or {}).get(team_key, default_node)),
            "slot": (team_slot or {}).get(team_key, 0 if placement else None),
        }
        for team_key, team in teams.items()
        for box_idx, box in enumerate(boxes)
    ]


def describe_target(t):
    return f"{t['team_key']}/{t['box_name']}  vmid {t['vmid']}  {t['ip']}"
