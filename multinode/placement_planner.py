"""Placement computation: node probes, capacity-fill team placement, decision table, and resolve_placement."""

import os
import time

from common.constants import ENGINE_TEMPLATE_VMID_OFFSET, MAX_BOXES_PER_TEAM, comp_tag
from multinode.nodes_config import (MAX_SATELLITES, NODES_CONFIG_PATH, PLACEMENT_VERSION,
                                    load_nodes_config)
from multinode.placement_record import (anchor_identifier, check_team_blocks,
                              default_jump_mgmt_ip, engine_record, golden_slot_span,
                              jump_vmid_for, read_placement, teams_on_node, write_placement)



def probe_node(record, need_templates, engine_vmid, team_identifiers, comp_name=""):
    from infra.client import proxmox_api_for
    probe = {"name": record.name, "node": record.node, "ok": True, "reasons": [],
             "missing_templates": [], "collisions": [], "mem_free_bytes": None,
             "datastore_free": None, "running_ours": 0, "templates": {}}
    ep, token = record.endpoint, os.environ.get(record.token_env, "")
    if not token:
        return {**probe, "ok": False,
                "reasons": [f"token env '{record.token_env}' empty"]}
    try:
        status = proxmox_api_for(ep, token, "GET", f"/nodes/{record.node}/status")["data"]
        vms = proxmox_api_for(ep, token, "GET", "/cluster/resources",
                              params={"type": "vm"})["data"]
        probe["mem_free_bytes"] = (status.get("memory") or {}).get("free")
        try:
            st = proxmox_api_for(ep, token, "GET",
                                 f"/nodes/{record.node}/storage/{record.datastore}/status")["data"]
            probe["datastore_free"] = st.get("avail")
        except Exception:
            probe["datastore_free"] = None
    except Exception as e:
        return {**probe, "ok": False, "reasons": [f"API unreachable: {str(e)[:120]}"]}

    for vm in vms:
        if vm.get("node") != record.node:
            continue
        tags = {t.strip() for t in str(vm.get("tags") or "").replace(";", ",").split(",") if t.strip()}
        if vm.get("template") == 1 and "template" in tags and vm.get("status") == "stopped":
            probe["templates"][vm.get("name")] = vm["vmid"]
        if "tezcatlipoca" in tags and vm.get("status") == "running":
            probe["running_ours"] += 1
    probe["missing_templates"] = sorted(set(need_templates) - set(probe["templates"]))

    vm_by_vmid = {vm.get("vmid"): vm for vm in vms if vm.get("node") == record.node}
    existing = set(vm_by_vmid)
    wanted = {engine_vmid, engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET}
    for slot in range(MAX_SATELLITES + 1):
        wanted.update(golden_slot_span(engine_vmid, slot))
        wanted.add(jump_vmid_for(engine_vmid, slot))
    for ident in team_identifiers:
        wanted.update(range(200 + int(ident) * 10, 200 + int(ident) * 10 + MAX_BOXES_PER_TEAM))
    # This run's own template slots (engine template + goldens) hold our build attempts and
    # kept artifacts, not collisions; only a foreign competition's converted template does.
    template_slots = {engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET}
    for slot in range(MAX_SATELLITES + 1):
        template_slots.update(golden_slot_span(engine_vmid, slot))

    def _not_a_collision(vmid):
        vm = vm_by_vmid.get(vmid) or {}
        if vmid not in template_slots:
            return False
        tags = {t.strip() for t in str(vm.get("tags") or "").replace(";", ",").split(",") if t.strip()}
        if vm.get("template") == 1 and vm.get("status") == "stopped":
            return "tezcatlipoca" in tags
        if any(t.startswith("comp-") and t != comp_tag(comp_name) for t in tags):
            return False
        return True

    probe["collisions"] = sorted(v for v in wanted & existing if not _not_a_collision(v))
    if probe["collisions"]:
        probe["ok"] = False
        probe["reasons"].append(f"vmid collisions: {probe['collisions'][:6]}"
                                + ("..." if len(probe["collisions"]) > 6 else ""))
    try:
        nets = proxmox_api_for(ep, token, "GET", f"/nodes/{record.node}/network")["data"]
        bridges = {n.get("iface") for n in nets}
        bad_bridges = [f"vmbr{i}" for i in team_identifiers if f"vmbr{i}" in bridges]
        if bad_bridges:
            probe["ok"] = False
            probe["reasons"].append(f"bridge collisions: {bad_bridges}")
    except Exception:
        pass
    return probe



def _team_reservation_mb(boxes):
    return sum(int(b.get("memory_mb") or 1024) for b in boxes)


def _prefer_index(name, prefer):
    return prefer.index(name) if name in prefer else len(prefer)


def compute_placement(records, balancing, teams, boxes, engine_vmid, comp_name,
                      team_overrides=None, engine_override=None, probes=None):
    team_overrides = dict(team_overrides or {})
    identifiers = {k: str(t["identifier"]) for k, t in teams.items()}
    by_identifier = {v: k for k, v in identifiers.items()}
    known = {r.name for r in records}
    resolved_overrides = {}
    for ident, node_name in team_overrides.items():
        key = by_identifier.get(ident, ident)
        if key not in teams:
            raise SystemExit(f"  ERROR: --team-node {ident}=... : no team with that "
                             f"key/identifier (teams: {', '.join(sorted(teams))})")
        if node_name not in known:
            raise SystemExit(f"  ERROR: --team-node {ident}={node_name}: no such node "
                             f"(nodes.json has: {', '.join(sorted(known))})")
        resolved_overrides[key] = node_name
    team_overrides = resolved_overrides

    need_templates = sorted({b["template"] for b in boxes})
    team_ids = sorted(set(identifiers.values()), key=int)
    probes = probes or {r.name: probe_node(r, need_templates, engine_vmid, team_ids,
                                            comp_name)
                        for r in records}

    team_nodes = dict(team_overrides)
    reserved_mb = {}
    for key, node_name in team_nodes.items():
        reserved_mb[node_name] = reserved_mb.get(node_name, 0) + _team_reservation_mb(boxes)

    prefer = [str(x) for x in (balancing.get("prefer") or [])]

    def eligible(rec):
        probe = probes[rec.name]
        if not probe["ok"]:
            return False
        if not {b["template"] for b in boxes} <= set(probe["templates"]):
            return False
        placed = sum(1 for n in team_nodes.values() if n == rec.name)
        return placed < rec.max_teams

    def eff_free(rec):
        mem = probes[rec.name].get("mem_free_bytes") or 0
        return (mem / rec.weight) - reserved_mb.get(rec.name, 0) * 1024 ** 2

    skipped = []
    for key in sorted((k for k in teams if k not in team_nodes),
                      key=lambda k: -_team_reservation_mb(boxes)):
        cands = [r for r in records if eligible(r)]
        if not cands:
            skipped.append(key)
            continue
        cands.sort(key=lambda r: (-eff_free(r), _prefer_index(r.name, prefer), r.name))
        team_nodes[key] = cands[0].name
        reserved_mb[cands[0].name] = (reserved_mb.get(cands[0].name, 0)
                                      + _team_reservation_mb(boxes))
    if skipped:
        detail = "; ".join(
            f"{r.name}: "
            + ", ".join(probes[r.name]["reasons"] or probes[r.name]["missing_templates"]
                        or ["under capacity"])
            for r in records)
        raise SystemExit(
            "  ERROR: no eligible node for team(s) " + ", ".join(sorted(skipped))
            + f". Node state: {detail}. Sync missing templates (sync-template.py), free "
              "capacity, or raise max_teams/weight in nodes.json.")

    if engine_override:
        if engine_override not in known:
            raise SystemExit(f"  ERROR: --engine-node {engine_override}: no such node")
        engine_node = engine_override
    else:
        counts = {r.name: sum(1 for n in team_nodes.values() if n == r.name)
                  for r in records}
        best_count = max(counts.values())
        cands = [r for r in records if counts[r.name] == best_count]
        cands.sort(key=lambda r: (_prefer_index(r.name, prefer),
                                  -(probes[r.name].get("mem_free_bytes") or 0), r.name))
        engine_node = cands[0].name

    used = [engine_node] + sorted(
        (n for n in set(team_nodes.values()) if n != engine_node),
        key=lambda n: (_prefer_index(n, prefer), n))
    if len(used) - 1 > MAX_SATELLITES:
        raise SystemExit(f"  ERROR: placement needs {len(used) - 1} satellites but "
                         f"MAX_SATELLITES={MAX_SATELLITES} (node routes are registered, not dynamic)")

    slots = {name: i for i, name in enumerate(used)}
    satellites = []
    seen_jump_ips = {}
    pseudo = {"team_nodes": team_nodes, "team_identifiers": identifiers}
    for name in used[1:]:
        slot = slots[name]
        rec = next(r for r in records if r.name == name)
        ip = rec.jump_mgmt_ip or default_jump_mgmt_ip(slot)
        if ip in seen_jump_ips:
            raise SystemExit(f"  ERROR: duplicate jump mgmt IP {ip} for nodes "
                             f"'{seen_jump_ips[ip]}' and '{name}' — set explicit "
                             "jump_mgmt_ip values in nodes.json")
        seen_jump_ips[ip] = name
        satellites.append({
            "name": name, "slot": slot,
            "teams": [k for k, n in team_nodes.items() if n == name],
            "jump_vmid": jump_vmid_for(engine_vmid, slot),
            "jump_mgmt_ip": ip,
            "anchor_identifier": anchor_identifier(pseudo, name),
        })

    placement = {
        "version": PLACEMENT_VERSION,
        "comp": comp_name,
        "engine_vmid": engine_vmid,
        "engine_node": engine_node,
        "nodes": {r.name: r.to_json() for r in records},
        "slots": slots,
        "team_nodes": team_nodes,
        "team_slots": {k: slots[n] for k, n in team_nodes.items()},
        "team_identifiers": identifiers,
        "satellites": satellites,
        "jump_mgmt_ips": {s["name"]: s["jump_mgmt_ip"] for s in satellites},
        "probe_summary": {
            r.name: {k: probes[r.name].get(k)
                     for k in ("ok", "reasons", "missing_templates", "collisions",
                               "mem_free_bytes", "datastore_free", "running_ours")}
            for r in records},
        "computed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    check_team_blocks(placement, teams)
    return placement


def placement_decision_table(placement, probes=None):
    probes = probes or placement.get("probe_summary") or {}
    lines = ["  Multi-node placement:"]
    eng = placement["engine_node"]
    eng_teams = teams_on_node(placement, eng)
    lines.append(f"    engine: {eng} (slot 0) — teams {', '.join(eng_teams) or '(none)'}")
    for sat in placement["satellites"]:
        lines.append(f"    satellite: {sat['name']} (slot {sat['slot']}) — "
                     f"teams {', '.join(sat['teams'])}, jump {sat['jump_mgmt_ip']} "
                     f"(vmid {sat['jump_vmid']})")
    for name, p in probes.items():
        mem = p.get("mem_free_bytes")
        mem_s = f"{mem / 1024 ** 3:.0f}G free" if mem else "mem unknown"
        extra = "" if p.get("ok") else f"  [!] {'; '.join(p.get('reasons') or [])}"
        lines.append(f"    probe {name}: {mem_s}, {p.get('running_ours', '?')} tezcatlipoca "
                     f"VM(s) running{extra}")
    return "\n".join(lines)



def resolve_placement(comp_dir, engine_vmid, teams, boxes, comp_name,
                      team_overrides=None, engine_override=None, resume_endpoint=None,
                      nodes_path=None):
# A recorded placement always wins; a resume with no record adopts its deployed endpoint
#     instead of re-balancing a live range (which would strand it).
    placement = read_placement(comp_dir)
    if placement:
        if set(placement["team_nodes"]) != set(teams):
            raise SystemExit(
                f"  ERROR: the placement was computed for teams "
                f"{sorted(placement['team_nodes'])} but this deploy has teams "
                f"{sorted(teams)} — the placement (and its satellite routing) no "
                "longer matches. Destroy and redeploy to re-place.")
        if team_overrides or engine_override:
            print("  a placement is already recorded — ignoring --team-node/--engine-node "
                  "(destroy and redeploy to re-place)")
        return placement, engine_record(placement)
    records, balancing = load_nodes_config(nodes_path)
    if records is None:
        if team_overrides or engine_override:
            # A pin with nothing to resolve it against would silently deploy to the
            # ambient single-node host.
            raise SystemExit(
                f"  ERROR: --engine-node/--team-node given but no nodes list resolved "
                f"({nodes_path or NODES_CONFIG_PATH} does not exist) — refusing to "
                "deploy single-node from .env. Pass --nodes <file> or drop the pins.")
        return None, None
    zero_base = [r.name for r in records if r.engine_base_vmid == 0]
    if zero_base:
        raise SystemExit(f"  ERROR: nodes.json node(s) {zero_base} lack engine_base_vmid")

    if resume_endpoint:
        match = next((r for r in records
                      if r.endpoint.rstrip("/") == resume_endpoint.rstrip("/")), None)
        if match is None:
            raise SystemExit(
                f"  ERROR: this competition's state records deploy endpoint "
                f"{resume_endpoint}, which is not in nodes.json — add that node or "
                "destroy and redeploy. Refusing to re-place a live range.")
        print(f"  Resume without a recorded placement — keeping the whole range on "
              f"'{match.name}' ({resume_endpoint})")
        probes = {r.name: probe_node(r, sorted({b["template"] for b in boxes}),
                                     engine_vmid,
                                     sorted({str(t["identifier"]) for t in teams.values()},
                                            key=int), comp_name)
                  for r in records}
        placement = compute_placement(
            records, balancing, teams, boxes, engine_vmid, comp_name,
            team_overrides={k: match.name for k in teams},
            engine_override=match.name, probes=probes)
        write_placement(comp_dir, placement)
        return placement, engine_record(placement)

    print("  Multi-node config found (nodes.json) — probing nodes and placing teams...")
    probes = {r.name: probe_node(
        r, sorted({b["template"] for b in boxes}), engine_vmid,
        sorted({str(t["identifier"]) for t in teams.values()}, key=int), comp_name)
        for r in records}
    placement = compute_placement(records, balancing, teams, boxes, engine_vmid,
                                  comp_name, team_overrides=team_overrides,
                                  engine_override=engine_override, probes=probes)
    print(placement_decision_table(placement, probes))
    print(f"  Placement recorded in {write_placement(comp_dir, placement)} "
          "(authoritative for resume/redeploy/verify/destroy)")
    return placement, engine_record(placement)
