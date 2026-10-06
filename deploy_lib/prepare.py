"""prepare(): everything deploy() must do before phase 1, as one ordered, testable step."""

from config_ops import confirm_deploy

from deploy_lib.configs import generate_stage_configs_and_hashes
from deploy_lib.context import DeployContext
from deploy_lib.gates import check_resume_gates, run_range_gates
from deploy_lib.inputs import (load_competition_inputs, load_competition_spec,
                               load_prior_deploy_state, resolve_engine_vmid)
from deploy_lib.placement import apply_engine_placement
from deploy_lib.secrets import mint_competition_secrets, resolve_competition_teams
from deploy_lib.stages import (CompetitionInputs, CompetitionSecrets, CompetitionSpec,
                               DeployTargets, EnginePlacement, GeneratedConfigs,
                               PriorDeployState, RunIdentity, TerraformInputs)
from deploy_lib.targets import enumerate_deploy_targets
from deploy_lib.tfinputs import build_terraform_inputs, engine_mgmt_ip_from_env


def prepare(comp_dir, num_teams=None, assume_yes=False, from_phase=1, scoring_vmid=None,
            team_node=None, engine_node=None, force_from_phase=False):
    """Everything deploy() must do before phase 1, as one testable step.

    Resolves the engine VMID, loads Compfile/config/state, resolves teams, computes
    multi-node placement, runs preflight, THEN mints the competition's secrets,
    generates the nakon + stage configs, computes the golden hashes and assembles
    terraform.tfvars.json, and enumerates the targets. Returns None when the operator
    declines the confirmation prompt, so deploy() can return without running any phase
    (the same early return the inline code had).

    The write lock is deliberately NOT taken here: deploy() holds it across the whole
    run, phases included. Nothing here destroys anything — the frozen gate runs before
    phase 1 precisely so a freeze can still say "nothing destroyed".

    Order note: preflight runs BEFORE the fresh credential mint. These gates can abort
    the deploy (engine mgmt IP collision, missing templates, datastore headroom, catalog
    errors) — a state file written with minted credentials no engine ever received is a
    credential-desync corpse: every login 401s against the live range until state is
    restored by hand (2026-09-30 testcomp-7box: a create-competition re-run against an
    already-deployed comp rotated state while the engine kept the originals). Resumes
    keep the gate where it was relative to their carried (not minted) credentials."""
    identity = RunIdentity()
    resolve_engine_vmid(identity, comp_dir, from_phase, scoring_vmid)
    # (The engine lock itself is taken further down — multi-node placement must point
    # the env at the engine's host first, and that needs teams+boxes resolved.)

    spec = CompetitionSpec()
    load_competition_spec(spec, comp_dir)

    prior = PriorDeployState()
    load_prior_deploy_state(prior, comp_dir, from_phase)
    # Gate 1 (gates.py): refuse an unsound resume before anything else keys on it.
    check_resume_gates(comp_dir, prior.state_path, prior.previous_state, from_phase,
                       force=force_from_phase)

    inputs = CompetitionInputs()
    load_competition_inputs(inputs, comp_dir)

    secrets = CompetitionSecrets()
    resolve_competition_teams(secrets, prior, spec, num_teams, identity, from_phase)

    place = EnginePlacement()
    apply_engine_placement(place, prior, secrets, spec, identity, comp_dir, team_node, engine_node)

    terraform = TerraformInputs()
    terraform.engine_mgmt_ip = engine_mgmt_ip_from_env()

    # Gate 2 (gates.py): stale-terraform-state refusal + capacity/collision preflight.
    run_range_gates(comp_dir, spec, secrets, identity, place, terraform, prior, from_phase)

    if not prior.resuming:
        mint_competition_secrets(secrets, prior, spec, inputs, identity)

    generated = GeneratedConfigs()
    generate_stage_configs_and_hashes(generated, comp_dir, spec, secrets)

    build_terraform_inputs(terraform, comp_dir, spec, secrets, identity, place)

    if not assume_yes and not prior.resuming:
        if not confirm_deploy(spec.name, spec.scenario, spec.difficulty, secrets.teams, spec.boxes):
            print("  Deployment cancelled.")
            return None

    targets = DeployTargets()
    enumerate_deploy_targets(targets, comp_dir, spec, secrets, place)

    return assemble_deploy_context(comp_dir, from_phase, assume_yes, identity, spec, prior,
                                    inputs, secrets, place, generated, terraform, targets)


def assemble_deploy_context(comp_dir, from_phase, assume_yes, identity, spec, prior, inputs,
                             secrets, place, generated, terraform, targets):
    """Expand the prepared stages onto DeployContext's flat fields.

    Kept out of prepare() so the sequencer reads as the ordered steps it is: this is the
    one place that maps the stage objects onto DeployContext, whose field order follows
    the pipeline."""
    return DeployContext(
        comp_dir=comp_dir,
        comp_name=spec.comp_name,
        state_path=prior.state_path,
        from_phase=from_phase,
        resuming=prior.resuming,
        assume_yes=assume_yes,
        name=spec.name,
        scenario=spec.scenario,
        box_username=spec.box_username,
        credlist_usernames=spec.credlist_usernames,
        nakon_jobs=spec.nakon_jobs,
        apt_cache=spec.apt_cache,
        injects=inputs.injects,
        packet_pw=inputs.packet_pw,
        state=secrets.state,
        teams=secrets.teams,
        number_of_teams=secrets.number_of_teams,
        admin_password=secrets.admin_password,
        scoring_password=secrets.scoring_password,
        postgres_password=secrets.postgres_password,
        redis_password=secrets.redis_password,
        box_password=secrets.box_password,
        box_creds=secrets.box_creds,
        domain_creds=secrets.domain_creds,
        inject_password=secrets.inject_password,
        placement=place.placement,
        node=targets.node,
        engine_vmid=identity.engine_vmid,
        engine_mgmt_ip=terraform.engine_mgmt_ip,
        boxes=spec.boxes,
        boxes_by_name={b["name"]: b for b in spec.boxes},
        unbooted=generated.unbooted,
        nakon_config_path=generated.nakon_config_path,
        golden_config_path=generated.golden_config_path,
        repair_config_path=generated.repair_config_path,
        final_config_path=generated.final_config_path,
        golden_inputs=generated.golden_inputs,
        golden_hashes=generated.golden_hashes,
        frozen_keep=generated.frozen_keep,
        tf_dir=terraform.tf_dir,
        tfvars_path=terraform.tfvars_path,
        tfvars=terraform.tfvars,
        ssh_key_abs=terraform.ssh_key_abs,
        all_targets=targets.all_targets,
        managed_targets=targets.managed_targets,
        linux_targets=targets.linux_targets,
        windows_targets=targets.windows_targets,
        run_id=secrets.run_id,
        reclaim_run_id=(prior.previous_state.get("run_id") or ""),
    )
