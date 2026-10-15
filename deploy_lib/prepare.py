"""prepare(): everything deploy() must do before phase 1, as one ordered, testable step."""

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


def prepare(comp_dir, num_teams=None, assume_yes=False, from_phase=1, scoring_vmid=None,
            team_node=None, engine_node=None, force_from_phase=False, nodes_path=None,
            team_identifiers=None, engine_mgmt_ip=None):
# Everything deploy() must do before phase 1, as one testable step.
#
#     Resolves the engine VMID, loads Compfile/config/state, resolves teams, computes
#     multi-node placement, runs preflight, THEN mints the competition's secrets,
#     generates the nakon + stage configs, computes the golden hashes and assembles
#     the state document, and enumerates the targets. Returns None when the operator
#     declines the confirmation prompt, so deploy() can return without running any phase
#     (the same early return the inline code had).
#
#     The write lock is deliberately NOT taken here: deploy() holds it across the whole
#     run, phases included. Nothing here destroys anything — and neither does phase 1;
#     the frozen gate still runs here so a config-class freeze refuses before any Proxmox
#     mutation.
#
#     Order note: preflight runs BEFORE the fresh credential mint. These gates can abort
#     the deploy (engine mgmt IP collision, missing templates, datastore headroom, catalog
#     errors) — a state file written with minted credentials no engine ever received is a
#     credential-desync corpse: every login 401s against the live range until state is
#     restored by hand (2026-09-30 testcomp-7box: a create-competition re-run against an
#     already-deployed comp rotated state while the engine kept the originals). Resumes
#     keep the gate where it was relative to their carried (not minted) credentials.
    ctx = DeployContext(comp_dir=comp_dir, from_phase=from_phase, assume_yes=assume_yes)
    resolve_engine_vmid(ctx, comp_dir, from_phase, scoring_vmid)
    # (The engine lock itself is taken further down — multi-node placement must point
    # the env at the engine's host first, and that needs teams+boxes resolved.)

    load_competition_spec(ctx, comp_dir)

    load_prior_deploy_state(ctx, comp_dir, from_phase)
    # Gate 1 (gates.py): refuse an unsound resume before anything else keys on it.
    check_resume_gates(comp_dir, ctx.state_path, ctx.previous_state, from_phase,
                       force=force_from_phase)

    load_competition_inputs(ctx, comp_dir)

    resolve_competition_teams(ctx, num_teams, from_phase, team_identifiers)

    apply_engine_placement(ctx, comp_dir, team_node, engine_node, nodes_path)

    ctx.engine_mgmt_ip = engine_mgmt_ip_from_env(engine_mgmt_ip)

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
