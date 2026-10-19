"""prepare(): everything deploy() must do before phase 1, as one ordered, testable step."""

import os

from config.config_prompts import confirm_deploy

from deploy_lib.configs import generate_stage_configs_and_hashes
from deploy_lib.context import DeployContext
from infra.gates import check_resume_gates, run_range_gates
from deploy_lib.inputs import (load_competition_inputs, load_competition_spec,
                               load_prior_deploy_state, resolve_engine_vmid)
from deploy_lib.placement import apply_engine_placement
from deploy_lib.secrets import mint_competition_secrets, resolve_competition_teams
from deploy_lib.targets import enumerate_deploy_targets
from deploy_lib.estate_inputs import build_estate_inputs, engine_mgmt_ip_from_env


def _select_engine_mgmt_ip(ctx):
# Scan every hosting node the placement covers; skip this run's own engine/jumps and claim
#     the gateway + jump addresses so an auto-selected engine never takes one of them.
    from infra import collisions
    from common.constants import DEFAULT_ENGINE_MGMT_GW
    from estate import config
    claimed = {config.active().engine.mgmt_gw or DEFAULT_ENGINE_MGMT_GW}
    skip = {ctx.engine_vmid}
    nodes = [config.active().proxmox.node]
    if ctx.placement:
        nodes = [rec["node"] for rec in ctx.placement["nodes"].values()]
        claimed |= {s["jump_mgmt_ip"] for s in ctx.placement.get("satellites") or []}
        skip |= {s["jump_vmid"] for s in ctx.placement.get("satellites") or []}
    return collisions.select_engine_ip(
        collisions.scan_vms_for(nodes), claimed, skip, ctx.comp_tags)


def _resolve_engine_mgmt_ip(ctx, override):
    from common.constants import DEFAULT_ENGINE_MGMT_GW
    from estate import config
    from multinode.placement_record import engine_record
    record = engine_record(ctx.placement) if ctx.placement else None
    recorded = ((record.engine_mgmt_ip if record else "")
                or ((ctx.placement or {}).get("engine_mgmt_ip") or ""))
    ip = override or recorded
    # Auto-select only on a fresh deploy where nobody chose an address. An incomplete
    # agent scan makes every candidate a guess, so the default is kept and the preflight
    # refuses (the decision beside ENGINE_MGMT_BAND).
    if (override is None and not recorded and not ctx.resuming
            and not config.active().is_set("engine.mgmt_ip")):
        selected, _unchecked = _select_engine_mgmt_ip(ctx)
        if selected:
            ip = selected
            # Marked so engine_mgmt_ip_gate cannot read this as an operator pin.
            os.environ["TEZ_ENGINE_MGMT_IP_IS_AUTO_SELECTED"] = "1"
            print(f"  Engine mgmt IP: {selected} (auto-selected — agent scan complete)")
    ip = engine_mgmt_ip_from_env(ip)
    gw = (config.active().engine.mgmt_gw or (record.engine_mgmt_gw if record else "")
          or DEFAULT_ENGINE_MGMT_GW)
    config.active().activate({"engine.mgmt_gw": gw})
    return ip


def _record_placement_identities(ctx):
# Write every identity this run will create into the placement, before preflight reads it.
    from estate import config
    from multinode.placement_record import record_placement_identities
    from red import red01_ops
    record_placement_identities(
        ctx.comp_dir, ctx.placement, ctx.engine_mgmt_ip, config.active().engine.mgmt_gw,
        red01=red01_ops.identity(
            ctx.comp_dir, ctx.engine_vmid, config.active().proxmox.node,
            placement=ctx.placement))


def prepare(comp_dir, num_teams=None, assume_yes=False, from_phase=1, scoring_vmid=None,
            team_node=None, engine_node=None, force_from_phase=False, nodes_path=None,
            team_identifiers=None, engine_mgmt_ip=None):
# Deliberately no write lock here: deploy() holds it across the whole run. The frozen
# gate still runs here so a config-class freeze refuses before any Proxmox mutation.
# Preflight runs BEFORE the credential mint: aborting after minting leaves state with
# creds no engine ever received — every login 401s until state is restored by hand.
    ctx = DeployContext(comp_dir=comp_dir, from_phase=from_phase, assume_yes=assume_yes)
    from estate import state as estate_state
    estate_state.clear_teardown_journal(comp_dir)
    resolve_engine_vmid(ctx, comp_dir, from_phase, scoring_vmid)
    # The engine lock is taken further down: placement must point the env at the engine's host first.

    load_competition_spec(ctx, comp_dir)

    load_prior_deploy_state(ctx, comp_dir, from_phase)
    # Gate 1 (gates.py): refuse an unsound resume before anything else keys on it.
    check_resume_gates(comp_dir, ctx.state_path, ctx.previous_state, from_phase,
                       force=force_from_phase)

    load_competition_inputs(ctx, comp_dir)

    resolve_competition_teams(ctx, num_teams, from_phase, team_identifiers)

    apply_engine_placement(ctx, comp_dir, team_node, engine_node, nodes_path)

    ctx.engine_mgmt_ip = _resolve_engine_mgmt_ip(ctx, engine_mgmt_ip)
    if ctx.placement:
        _record_placement_identities(ctx)
    # The banner above printed the pre-placement, pre-flag target; restate it now that it
    # is real (caught live: a canary dialed .244 while the banner said .252).
    from common.utils import env_summary
    print(f"  Effective target: {env_summary()}")

    # Gate 2 (gates.py): stale-estate refusal + capacity/collision preflight.
    run_range_gates(ctx, comp_dir, from_phase)

    if not ctx.resuming:
        mint_competition_secrets(ctx)

    generate_stage_configs_and_hashes(ctx, comp_dir)

    build_estate_inputs(ctx, comp_dir)

    if not assume_yes and not ctx.resuming:
        if not confirm_deploy(ctx.name, ctx.scenario, ctx.difficulty, ctx.teams, ctx.boxes):
            print("  Deployment cancelled.")
            return None

    enumerate_deploy_targets(ctx, comp_dir)

    ctx.boxes_by_name = {b["name"]: b for b in ctx.boxes}
    ctx.reclaim_run_id = ctx.previous_state.get("run_id") or ""
    return ctx
