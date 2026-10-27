"""The authoritative per-competition placement record: read/write, accessors, process-wide activation, and the vmid slot math derived from it."""

import os

from common.constants import ENGINE_TEMPLATE_VMID_OFFSET, GOLDEN_VMID_OFFSET, MAX_BOXES_PER_TEAM
from multinode.nodes_config import (JUMP_MGMT_IP_BASE, JUMP_VMID_OFFSET, NodeRecord,
                          PLACEMENT_VERSION, apply_node_env, restore_node_env)



def read_placement(comp_dir):
# A document from placement schema version 1 upgrades on read and is written back, so a companion op (teardown above all)
#     can still find the red01 that range deployed; an unknown version is refused.
    from estate import state
    placement = state.EstateState.load(comp_dir).data.get("placement")
    if not placement:
        return None
    version = placement.get("version")
    if version == PLACEMENT_VERSION:
        return placement
    if version == 1:
        upgraded = _upgrade_legacy(comp_dir, placement)
        write_placement(comp_dir, upgraded)
        return upgraded
    raise SystemExit(f"  ERROR: the placement record was written by schema v"
                     f"{version}; this code understands "
                     f"v{PLACEMENT_VERSION}. Tear the competition down and redeploy.")


def _upgrade_legacy(comp_dir, placement):
# Resolve red01 the legacy way — that is where a pre-ownership red01 sits.
    from estate import state
    from red import red01_ops
    upgraded = dict(placement)
    engine_vmid = int(upgraded["engine_vmid"])
    engine = engine_record(upgraded)
    flat = state.load_flat(comp_dir)
    upgraded["version"] = PLACEMENT_VERSION
    upgraded["engine_template_vmid"] = engine_template_vmid(engine_vmid)
    upgraded["golden_vmids"] = _golden_vmids(upgraded)
    upgraded["engine_mgmt_ip"] = flat.get("engine_mgmt_ip") or ""
    upgraded["engine_mgmt_gw"] = flat.get("engine_mgmt_gw") or ""
    upgraded["mgmt_bridge"] = mgmt_bridge_for(engine)
    upgraded["red01"] = {
        "vmid": red01_ops.red01_vmid_for(comp_dir, engine_vmid, upgraded),
        "ip": red01_ops.red01_ip_for(comp_dir, upgraded),
        "node": engine.node,
        "explicit_ip": red01_ops.red01_ip_is_explicit(comp_dir),
    }
    return upgraded


def write_placement(comp_dir, placement):
    from estate import state
    document = state.EstateState.load(comp_dir)
    document.data["placement"] = placement
    document.save()
    return state.state_path(comp_dir)


def engine_template_vmid(engine_vmid):
    return int(engine_vmid) + ENGINE_TEMPLATE_VMID_OFFSET


def mgmt_bridge_for(record=None):
# The node's own bridge when its L2 is not vmbr0 (the .67 cyber range's is vmbr100), else
#     the profile's.
    from estate import config
    if record is not None and record.mgmt_bridge:
        return record.mgmt_bridge
    return config.active().proxmox.mgmt_bridge or "vmbr0"


def resolved_mgmt_bridge(placement=None):
    return (placement or {}).get("mgmt_bridge") or mgmt_bridge_for()


def _golden_vmids(placement):
    return {name: golden_slot_span(placement["engine_vmid"], slot)
            for name, slot in placement["slots"].items()}


def record_placement_identities(comp_dir, placement, engine_mgmt_ip="", engine_mgmt_gw="",
                                red01=None):
# Write every identity this run will create once, so preflight/resume/verify/teardown cannot
#     disagree about a vmid.
    placement["version"] = PLACEMENT_VERSION
    placement["engine_template_vmid"] = engine_template_vmid(placement["engine_vmid"])
    placement["golden_vmids"] = _golden_vmids(placement)
    placement["engine_mgmt_ip"] = engine_mgmt_ip or ""
    placement["engine_mgmt_gw"] = engine_mgmt_gw or ""
    placement["mgmt_bridge"] = mgmt_bridge_for(engine_record(placement))
    if red01 is not None:
        placement["red01"] = red01
    write_placement(comp_dir, placement)
    return placement


def write_red01_ip(comp_dir, placement, ip):
# explicit_ip stays false: a selected address was proven free when chosen, unlike an
#     operator's value.
    placement.setdefault("red01", {})["ip"] = ip
    placement["red01"]["explicit_ip"] = False
    write_placement(comp_dir, placement)
    return ip


def reserved_vmids(placement):
    from red.red01_ops import default_red_vmid
    engine_vmid = int(placement["engine_vmid"])
    # The RECORDED red01 wins: a Compfile red01_vmid is not the derived default, and missing
    # it let a team block collide with a red01 reserve preflight had already honoured.
    recorded_red = (placement.get("red01") or {}).get("vmid")
    reserved = {engine_vmid, engine_template_vmid(engine_vmid),
                int(recorded_red) if recorded_red
                else default_red_vmid(engine_vmid, len(placement.get("satellites") or []))}
    for slot in set((placement.get("slots") or {}).values()):
        reserved.update(golden_slot_span(engine_vmid, slot))
    for sat in placement.get("satellites") or []:
        reserved.add(int(sat["jump_vmid"]))
    return reserved


def check_team_blocks(placement, teams):
# collect_teams validates slot-0 bands only and runs before the slot count exists.
    reserved = reserved_vmids(placement)
    for key, team in teams.items():
        identifier = team["identifier"]
        base = 200 + int(identifier) * 10
        block = set(range(base, base + MAX_BOXES_PER_TEAM))
        clash = sorted(block & reserved)
        if clash:
            raise SystemExit(
                f"  ERROR: team {key} (identifier {identifier}) maps to vmids "
                f"{base}..{base + MAX_BOXES_PER_TEAM - 1}, overlapping this placement's own "
                f"reserved vmids {clash} (engine, engine template, a slot's goldens, a jump or "
                f"red01). Pick a team-identifier block clear of the whole engine span, or a "
                f"different --scoring-vmid.")


def record_of(placement, node_name):
    return NodeRecord.from_json(placement["nodes"][node_name])


def engine_record(placement):
    return record_of(placement, placement["engine_node"])


def node_of_team(placement, team_key):
    return placement["team_nodes"][team_key]


def slot_of_team(placement, team_key):
    return placement["slots"][node_of_team(placement, team_key)]


def teams_on_node(placement, node_name):
    return sorted(k for k, n in placement["team_nodes"].items() if n == node_name)


def anchor_identifier(placement, node_name):
    teams = teams_on_node(placement, node_name)
    if not teams:
        return None
    ids = {k: placement["team_identifiers"][k] for k in teams}
    return ids[min(ids, key=lambda k: int(ids[k]))]


def activate_placement(placement):
    from infra.client import register_node_routes
    engine = engine_record(placement)
    restore = apply_node_env(engine)
    # Keyed by PVE host name, not the nodes.json record name: every node-scoped API path
    # uses /nodes/<pve-name>/...
    routes = {}
    for raw in placement["nodes"].values():
        rec = NodeRecord.from_json(raw)
        token = os.environ.get(rec.token_env)
        if not token:
            raise SystemExit(
                f"  ERROR: nodes.json node '{rec.name}' points at token_env "
                f"'{rec.token_env}' but that env var is empty/absent — set the token in "
                f"the environment (never in nodes.json, which is committed).")
        routes[rec.node] = (rec.endpoint, token, rec.tls_fingerprint or "")
    register_node_routes(routes)
    return restore


def deactivate_placement(restore):
    from infra.client import clear_node_routes
    clear_node_routes()
    restore_node_env(restore)



def golden_vmid_for_slot(engine_vmid, slot, box_idx):
# Slot 0 keeps engine_vmid+150+i; each satellite shifts one box-stride so the same box type
#     can be a golden on several hosts (linked clones cannot cross nodes).
    return int(engine_vmid) + GOLDEN_VMID_OFFSET + slot * MAX_BOXES_PER_TEAM + box_idx


def jump_vmid_for(engine_vmid, slot):
    return int(engine_vmid) + JUMP_VMID_OFFSET + slot


def default_jump_mgmt_ip(slot):
    octet = int(JUMP_MGMT_IP_BASE.split(".")[-1]) - (slot - 1)
    if octet < 1:
        raise SystemExit(f"  ERROR: jump mgmt IP range exhausted at slot {slot} — "
                         "set explicit jump_mgmt_ip values in nodes.json")
    return ".".join(JUMP_MGMT_IP_BASE.split(".")[:3]) + f".{octet}"


def golden_slot_span(engine_vmid, slot):
    return [golden_vmid_for_slot(engine_vmid, slot, i) for i in range(MAX_BOXES_PER_TEAM)]




def satellite_routes_for(placement):
# One route per team subnet behind each satellite's jump: the engine's default gw blackholes
#     the un-routed subnets. Re-asserted by a oneshot unit so an engine reboot keeps them.
    routes = []
    for sat in placement["satellites"]:
        anchor = sat.get("anchor_identifier")
        if anchor:
            routes.append({"subnet": f"192.168.{anchor}.0/24", "via": sat["jump_mgmt_ip"]})
        for k in sat.get("teams") or []:
            ident = placement["team_identifiers"][k]
            route = {"subnet": f"192.168.{ident}.0/24", "via": sat["jump_mgmt_ip"]}
            if route not in routes:
                routes.append(route)
    return routes
