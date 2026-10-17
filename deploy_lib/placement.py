"""Multi-node placement: parse --team-node, resolve placement, take the engine lock."""


from nakonops.nakon_lock_ops import acquire_engine_lock
from multinode.placement_record import activate_placement
from multinode.placement_planner import resolve_placement


def parse_team_node(team_node):
# --team-node '103=zfs-193,team4=hdd-150' -> {key: node}; key is a team id or subnet.
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
    # Env must point at the engine's host before the endpoint-keyed lock and any node-scoped
    # call, so placement resolves first. A recorded placement wins; a resume without one
    # adopts its deployed endpoint instead of re-balancing a live range.
    ctx.placement, _engine_record = resolve_placement(
        comp_dir, ctx.engine_vmid, ctx.teams, ctx.boxes, ctx.comp_name,
        team_overrides=parse_team_node(team_node), engine_override=engine_node,
        resume_endpoint=(ctx.previous_state.get("deployed_endpoint") if ctx.resuming else None),
        nodes_path=nodes_path)
    if ctx.placement:
        activate_placement(ctx.placement)
        # The banner printed the pre-placement env and reads as the wrong host on a
        # --nodes run; restate the target now that it is real.
        from common.utils import env_summary
        print(f"  Effective target: {env_summary()}")
    ctx.state["multi_node"] = bool(ctx.placement)
    ctx.save_state()
    acquire_engine_lock(ctx.engine_vmid)
