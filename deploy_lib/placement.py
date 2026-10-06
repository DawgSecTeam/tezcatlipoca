"""Multi-node placement: parse --team-node, resolve placement, take the engine lock."""

import os

from config_ops import write_state
from nakon_ops import acquire_engine_lock
from nodes_ops import activate_placement, resolve_placement


def parse_team_node(team_node):
    """--team-node '103=zfs-193,team4=hdd-150' -> dict; team keys or subnet identifiers."""
    if not team_node:
        return None
    if isinstance(team_node, dict):
        return team_node
    out = {}
    for pair in str(team_node).split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise SystemExit(f"  ERROR: --team-node entry {pair!r} is not TEAM=NODE")
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def apply_engine_placement(place, prior, secrets, spec, identity, comp_dir, team_node, engine_node):
    """Resolve multi-node placement, point the env at the engine's host, take the lock.

    Resolution happens now because teams and boxes are known and the Proxmox env must
    point at the engine's host BEFORE the endpoint-keyed lock and any node-scoped call.
    The lock is taken last, on the identity prepare() resolved first; it stays held for
    the whole process (deploy() relies on that)."""
    # Multi-node placement (no-op without nodes.json/placement.json): resolved now —
    # teams and boxes are known, and the env must point at the engine's host BEFORE
    # the endpoint-keyed engine lock and anything node-scoped. An existing
    # placement.json always wins (authoritative); a resume without one adopts its
    # deployed endpoint rather than re-balancing a live range.
    place.placement, _resolved_engine_record = resolve_placement(
        comp_dir, identity.engine_vmid, secrets.teams, spec.boxes, spec.comp_name,
        team_overrides=parse_team_node(team_node), engine_override=engine_node,
        resume_endpoint=(prior.previous_state.get("deployed_endpoint") if prior.resuming else None))
    if place.placement:
        # Stays active for the whole deploy: every later node-scoped call and the
        # terraform env point at the placement's hosts.
        activate_placement(place.placement)
        if _resolved_engine_record is not None and _resolved_engine_record.engine_mgmt_ip:
            os.environ["TF_VAR_engine_mgmt_ip"] = _resolved_engine_record.engine_mgmt_ip
            if _resolved_engine_record.engine_mgmt_gw:
                os.environ.setdefault("TF_VAR_engine_mgmt_gw",
                                      _resolved_engine_record.engine_mgmt_gw)
    secrets.state["multi_node"] = bool(place.placement)
    write_state(prior.state_path, secrets.state)
    acquire_engine_lock(identity.engine_vmid)
