"""Multi-node placement: parse --team-node, resolve placement, take the engine lock."""


from nakonops.nakon_lock_ops import acquire_engine_lock
from multinode.placement_record import activate_placement
from multinode.placement_planner import resolve_placement
from estate import config


def parse_team_node(team_node):
# --team-node '103=zfs-193,team4=hdd-150' -> dict; team keys or subnet identifiers.
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


def apply_engine_placement(ctx, comp_dir, team_node, engine_node, nodes_path=None):
# Resolve multi-node placement, point the env at the engine's host, take the lock.
#
#     Resolution happens now because teams and boxes are known and the Proxmox env must
#     point at the engine's host BEFORE the endpoint-keyed lock and any node-scoped call.
#     The lock is taken last, on the engine vmid prepare() resolved first; it stays held for
#     the whole process (deploy() relies on that).
    # Multi-node placement (no-op without nodes.json/a recorded placement): resolved now —
    # teams and boxes are known, and the env must point at the engine's host BEFORE
    # the endpoint-keyed engine lock and anything node-scoped. An existing
    # the recorded placement always wins (authoritative); a resume without one adopts its
    # deployed endpoint rather than re-balancing a live range.
    ctx.placement, _resolved_engine_record = resolve_placement(
        comp_dir, ctx.engine_vmid, ctx.teams, ctx.boxes, ctx.comp_name,
        team_overrides=parse_team_node(team_node), engine_override=engine_node,
        resume_endpoint=(ctx.previous_state.get("deployed_endpoint") if ctx.resuming else None),
        nodes_path=nodes_path)
    if ctx.placement:
        # Stays active for the whole deploy: every later node-scoped call and the
        # node-scoped API calls and the process config point at the placement's hosts.
        activate_placement(ctx.placement)
        # The entry banner printed the pre-placement env, which reads as the wrong host on
        # a --nodes/multi-node run; restate the target now that it is the real one.
        from common.utils import env_summary
        print(f"  Effective target: {env_summary()}")
        if _resolved_engine_record is not None and _resolved_engine_record.engine_mgmt_ip:
            config.active().activate(
                {"engine.mgmt_ip": _resolved_engine_record.engine_mgmt_ip})
            if _resolved_engine_record.engine_mgmt_gw and not config.active().engine.mgmt_gw:
                config.active().activate(
                    {"engine.mgmt_gw": _resolved_engine_record.engine_mgmt_gw})
    ctx.state["multi_node"] = bool(ctx.placement)
    ctx.save_state()
    acquire_engine_lock(ctx.engine_vmid)
