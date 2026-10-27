"""(team, box) target abstraction: vmid arithmetic, enumeration, the frozen roster."""

def vm_id_for(identifier, box_index):
    return 200 + int(identifier) * 10 + box_index


def box_index(boxes, box_name):
    for i, b in enumerate(boxes):
        if b["name"] == box_name:
            return i
    raise KeyError(f"box {box_name!r} not in boxes list")


def persist_targets(comp_dir, targets, boxes):
# Freeze (team,box)->vmid at deploy so later tools don't recompute it from boxes.json order.
    from estate import state
    document = state.EstateState.load(comp_dir)
    document.section("roster").update({
        "box_order": [b["name"] for b in boxes],
        "targets": {
            t["vm_name"]: {
                "team_key": t["team_key"],
                "identifier": t["identifier"],
                "box_name": t["box_name"],
                "vmid": t["vmid"],
                "ip": t["ip"],
                # The hosting node is frozen too: a multi-node range's box is only reachable
                # through the host it lives on, and re-deriving it needs the placement.
                "node": t.get("node") or "",
                "slot": t.get("slot") or 0,
            }
            for t in targets
        },
    })
    document.save()


def frozen_roster(comp_dir):
    from estate import state
    roster = state.EstateState.load(comp_dir).section("roster")
    if not roster.get("targets"):
        return None
    return {"box_order": roster.get("box_order") or [], "targets": roster["targets"]}


def target_nodes(comp_dir):
    """The hosting node of every frozen (team, box), for callers that have no target dict."""
    data = frozen_roster(comp_dir) or {"targets": {}}
    return {(t["team_key"], t["box_name"]): (t.get("node") or "")
            for t in data["targets"].values()}


def load_targets(comp_dir, teams, boxes):
# Refuses when boxes.json was reordered since deploy: vmid is positional, so a reorder
# silently retargets a different VM.
    from estate import config, state
    placement = state.EstateState.load(comp_dir).data.get("placement")
    default_node = config.active().proxmox.node
    data = frozen_roster(comp_dir)
    if data is None:
        print("  (no frozen roster yet — deriving vmids from boxes.json order)")
        return enumerate_targets(teams, boxes, placement=placement, default_node=default_node)
    current = [b["name"] for b in boxes]
    frozen_order = data.get("box_order") or []
    # Append-only is safe (vmid is positional, added boxes land after every frozen index); a
    # reorder or a removal must refuse.
    if current[:len(frozen_order)] != frozen_order:
        raise SystemExit(
            f"  ERROR: boxes.json box order changed since deploy (was {frozen_order}, "
            f"now {current}). VM identity (vmid) is positional — restore the original order in "
            "boxes.json before redeploy/verify, or tear down and redeploy from scratch."
        )
    frozen = data.get("targets", {})
    result = []
    for t in enumerate_targets(teams, boxes, placement=placement, default_node=default_node):
        f = frozen.get(t["vm_name"])
        if f is not None:
            t = {**t, "vmid": f["vmid"], "ip": f.get("ip", t["ip"]),
                 # Dropping these sent every satellite box's snapshot/guest-agent call to the
                 # engine's host.
                 "node": f.get("node") or t["node"],
                 "slot": f.get("slot", t["slot"])}
        result.append(t)
    return result


def enumerate_targets(teams, boxes, placement=None, default_node=None):
    team_node = placement.get("team_nodes") if placement else None
    team_slot = placement.get("team_slots") if placement else None

    def _pve_node(name):
        # Placement keys are nodes.json NAMES; the API path needs the PVE hostname inside that record.
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
