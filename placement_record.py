"""placement.json (the authoritative per-competition placement record): read/write, accessors,
process-wide activation, and the vmid slot math derived from a placement."""

import json
import os

from constants import GOLDEN_VMID_OFFSET, MAX_BOXES_PER_TEAM
from nodes_config import (JUMP_MGMT_IP_BASE, JUMP_VMID_OFFSET, MAX_SATELLITES, NodeRecord,
                          PLACEMENT_VERSION, apply_node_env, restore_node_env)
from pathlib import Path


# ---------------------------------------------------------------- placement record

def read_placement(comp_dir):
    path = Path(comp_dir) / "placement.json"
    if not path.exists():
        return None
    try:
        placement = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"  ERROR: {path} is unreadable/malformed ({str(e)[:120]}) — "
                         "this competition was placed multi-node; the placement record "
                         "is authoritative for every op against it")
    if placement.get("version") != PLACEMENT_VERSION:
        raise SystemExit(f"  ERROR: {path} was written by placement schema v"
                         f"{placement.get('version')}; this code understands "
                         f"v{PLACEMENT_VERSION}. Tear the competition down and redeploy.")
    return placement


def write_placement(comp_dir, placement):
    path = Path(comp_dir) / "placement.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(placement, indent=2))
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o644)  # token references only, never token values
    except OSError:
        pass
    return path


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
    """The team subnet this node's golden boxes sit on: the first (lowest-identifier)
    team placed there. Slot 0 anchors on team1 — today's golden math exactly."""
    teams = teams_on_node(placement, node_name)
    if not teams:
        return None
    ids = {k: placement["team_identifiers"][k] for k in teams}
    return ids[min(ids, key=lambda k: int(ids[k]))]


def activate_placement(placement):
    """Make the whole process multi-node-aware: apply the engine node's env (terraform
    + every env-sourced read) and register the node->endpoint route table that routes
    node-scoped proxmox_api calls to the owning host. Returns the env restore dict."""
    from pve_api import register_node_routes
    engine = engine_record(placement)
    restore = apply_node_env(engine)
    # Keyed by PVE host name ("proxmox"/"pve") — every node-scoped API path uses
    # /nodes/<pve-name>/..., not the nodes.json record name.
    routes = {rec["node"]: (rec["endpoint"], os.environ[rec["token_env"]])
              for rec in placement["nodes"].values()}
    register_node_routes(routes)
    return restore


def deactivate_placement(restore):
    from pve_api import clear_node_routes
    clear_node_routes()
    restore_node_env(restore)


# ---------------------------------------------------------------- vmid slot math

def golden_vmid_for_slot(engine_vmid, slot, box_idx):
    """Golden blocks per node slot: slot 0 keeps the historical engine_vmid+150+i;
    each satellite shifts by one box-stride so the same box type can exist as a
    golden on several hosts (linked clones can't cross nodes on separate storages).
    vmids only need uniqueness per PVE instance — the stride exists so this also
    holds on a future single-cluster backend."""
    return int(engine_vmid) + GOLDEN_VMID_OFFSET + slot * MAX_BOXES_PER_TEAM + box_idx


def jump_vmid_for(engine_vmid, slot):
    return int(engine_vmid) + JUMP_VMID_OFFSET + slot


def default_jump_mgmt_ip(slot):
    """10.0.0.249 for slot 1, .248 for slot 2, ..."""
    octet = int(JUMP_MGMT_IP_BASE.split(".")[-1]) - (slot - 1)
    if octet < 1:
        raise SystemExit(f"  ERROR: jump mgmt IP range exhausted at slot {slot} — "
                         "set explicit jump_mgmt_ip values in nodes.json")
    return ".".join(JUMP_MGMT_IP_BASE.split(".")[:3]) + f".{octet}"


def golden_slot_span(engine_vmid, slot):
    return [golden_vmid_for_slot(engine_vmid, slot, i) for i in range(MAX_BOXES_PER_TEAM)]


# ---------------------------------------------------------------- tfvars helpers

def satellite_tfvars(placement):
    """Per-slot provider settings for terraform (tfvars.json): exactly MAX_SATELLITES
    entries; unused slots get dummies so the aliased providers never configure. Token
    values resolve from env at write time (tfvars.json is 0600)."""
    out = []
    records = {r.name: r for r in (NodeRecord.from_json(v)
                                   for v in placement["nodes"].values())}
    by_slot = {v: k for k, v in placement["slots"].items()}
    for i in range(1, MAX_SATELLITES + 1):
        name = by_slot.get(i)
        if name:
            rec = records[name]
            token = os.environ.get(rec.token_env)
            if not token:
                raise SystemExit(f"  ERROR: token env '{rec.token_env}' for node "
                                 f"'{name}' is empty — add it to .env")
            out.append({"endpoint": rec.endpoint, "api_token": token, "node": rec.node,
                        "datastore": rec.datastore})
        else:
            # bpg configures every DECLARED provider even with zero resources, so the
            # dummy must pass token-format validation (it never connects).
            out.append({"endpoint": f"https://sat{i}.invalid",
                        "api_token": "root@pam!dummy=00000000-0000-0000-0000-000000000000",
                        "node": "unused", "datastore": "unused"})
    return out


def satellite_routes_for(placement):
    """Engine static routes: one per TEAM SUBNET behind each satellite's jump (not
    just the golden anchor — every team box on the satellite is reached through the
    same jump, and the engine's default gw blackholes the un-routed subnets).
    Written into the engine by the team_nics provisioner (trigger-keyed, so changed
    routes re-apply on the next apply #1)."""
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
