"""The authoritative per-competition placement record (the document's `placement` section):
read/write, accessors, process-wide activation, and the vmid slot math derived from it."""

import os

from common.constants import GOLDEN_VMID_OFFSET, MAX_BOXES_PER_TEAM
from multinode.nodes_config import (JUMP_MGMT_IP_BASE, JUMP_VMID_OFFSET, NodeRecord,
                          PLACEMENT_VERSION, apply_node_env, restore_node_env)



def read_placement(comp_dir):
# The placement record from the document's `placement` section, or None when unplaced.
    from estate import state
    placement = state.EstateState.load(comp_dir).data.get("placement")
    if not placement:
        return None
    if placement.get("version") != PLACEMENT_VERSION:
        raise SystemExit(f"  ERROR: the placement record was written by schema v"
                         f"{placement.get('version')}; this code understands "
                         f"v{PLACEMENT_VERSION}. Tear the competition down and redeploy.")
    return placement


def write_placement(comp_dir, placement):
    from estate import state
    document = state.EstateState.load(comp_dir)
    document.data["placement"] = placement
    document.save()
    return state.state_path(comp_dir)


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
# The team subnet this node's golden boxes sit on: the first (lowest-identifier)
#     team placed there. Slot 0 anchors on team1 — today's golden math exactly.
    teams = teams_on_node(placement, node_name)
    if not teams:
        return None
    ids = {k: placement["team_identifiers"][k] for k in teams}
    return ids[min(ids, key=lambda k: int(ids[k]))]


def activate_placement(placement):
# Make the whole process multi-node-aware: apply the engine node's config (every
#     config-sourced read) and register the node->endpoint route table that routes
#     node-scoped proxmox_api calls to the owning host. Returns the env restore dict.
    from infra.client import register_node_routes
    engine = engine_record(placement)
    restore = apply_node_env(engine)
    # Keyed by PVE host name ("proxmox"/"pve") — every node-scoped API path uses
    # /nodes/<pve-name>/..., not the nodes.json record name.
    routes = {}
    for rec in placement["nodes"].values():
        token = os.environ.get(rec["token_env"])
        if not token:
            # A blank token used to surface as a 401 mid-preflight on the far node
            # (scale8 soak 2026-10-02) with the token-env collision as its cause. Refuse
            # here instead, naming the node and the variable.
            raise SystemExit(
                f"  ERROR: nodes.json node '{rec['name']}' points at token_env "
                f"'{rec['token_env']}' but that env var is empty/absent — set the token in "
                f"the environment (never in nodes.json, which is committed).")
        routes[rec["node"]] = (rec["endpoint"], token)
    register_node_routes(routes)
    return restore


def deactivate_placement(restore):
    from infra.client import clear_node_routes
    clear_node_routes()
    restore_node_env(restore)



def golden_vmid_for_slot(engine_vmid, slot, box_idx):
# Golden blocks per node slot: slot 0 keeps the historical engine_vmid+150+i;
#     each satellite shifts by one box-stride so the same box type can exist as a
#     golden on several hosts (linked clones can't cross nodes on separate storages).
#     vmids only need uniqueness per PVE instance — the stride exists so this also
#     holds on a future single-cluster backend.
    return int(engine_vmid) + GOLDEN_VMID_OFFSET + slot * MAX_BOXES_PER_TEAM + box_idx


def jump_vmid_for(engine_vmid, slot):
    return int(engine_vmid) + JUMP_VMID_OFFSET + slot


def default_jump_mgmt_ip(slot):
# 10.0.0.249 for slot 1, .248 for slot 2, ...
    octet = int(JUMP_MGMT_IP_BASE.split(".")[-1]) - (slot - 1)
    if octet < 1:
        raise SystemExit(f"  ERROR: jump mgmt IP range exhausted at slot {slot} — "
                         "set explicit jump_mgmt_ip values in nodes.json")
    return ".".join(JUMP_MGMT_IP_BASE.split(".")[:3]) + f".{octet}"


def golden_slot_span(engine_vmid, slot):
    return [golden_vmid_for_slot(engine_vmid, slot, i) for i in range(MAX_BOXES_PER_TEAM)]




def satellite_routes_for(placement):
# Engine static routes: one per TEAM SUBNET behind each satellite's jump (not
#     just the golden anchor — every team box on the satellite is reached through the
#     same jump, and the engine's default gw blackholes the un-routed subnets).
#     Written into the engine by the estate component's engine-network setup, and
#     re-asserted by a oneshot unit so an engine reboot mid-event keeps them.
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
