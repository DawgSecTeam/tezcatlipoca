"""deploy(): the sequencer. Takes the lock, prepares, walks PHASES, checkpoints, reports."""

from deploy_lib import phases
from deploy_lib.failure import record_degradations
from infra.gates import acquire_deploy_lock
from deploy_lib.prepare import prepare


def deploy(comp_dir, num_teams=None, assume_yes=False, from_phase=1, scoring_vmid=None,
           team_node=None, engine_node=None, force_from_phase=False, nodes_path=None,
           team_identifiers=None, engine_mgmt_ip=None):
# Run the eight-phase deploy for one competition; from_phase > 1 resumes from .deploy_state.json.
#
#     scoring_vmid overrides the scoring-engine VMID (default 1000) so several
#     competitions can run concurrently on one node; it is persisted to
#     .deploy_state.json and reused on resume.
#     team_node (--team-node id=NODE,...) and engine_node (--engine-node NAME) pin the
#     multi-node placement when nodes.json exists; without them teams are placed by
#     capacity-fill. force_from_phase (--force-from-phase) bypasses the guard that refuses
#     to skip phases .deploy_state.json never recorded as completed.
    acquire_deploy_lock(comp_dir)

    ctx = prepare(comp_dir, num_teams=num_teams, assume_yes=assume_yes, from_phase=from_phase,
                  scoring_vmid=scoring_vmid, team_node=team_node, engine_node=engine_node,
                  force_from_phase=force_from_phase, nodes_path=nodes_path,
                  team_identifiers=team_identifiers, engine_mgmt_ip=engine_mgmt_ip)
    if ctx is None:
        return

    run_pipeline(ctx)

    record_degradations(ctx)
    ctx.save_state()
    phases.finish_deploy(ctx)


def run_pipeline(ctx):
# Walk PHASES: call each, checkpoint the ones that ran, hook the terraform read.
#
#     Any failure is recorded (streak + degradations) and re-raised with a resume hint.
    current_phase = max(ctx.from_phase, 1)
    try:
        for n, phase in enumerate(phases.PHASES, 1):
            # Every phase is CALLED so it can print its own "[N/8] Skipped (resume)"
            # banner, but only a phase that actually ran may become the failure's
            # phase or write last_phase: checkpointing a skipped phase would move the
            # resume guard's answer forward for a phase this run never executed.
            running = ctx.from_phase <= n
            if running:
                current_phase = n
            phase(ctx)
            if running:
                ctx.checkpoint(n)
            if n == 2:
                # The terraform/SSH context is read once, exactly where the inline
                # code read it: after phase 2's checkpoint, before phase 3. A fresh
                # run only has terraform's outputs after apply #1; a --from-phase 3+
                # resume skips the apply but still needs them. Outside the `running`
                # gate on purpose, so a failure here reports the phase the operator
                # asked to resume at rather than phase 2.
                phases.connect_terraform(ctx)
    except BaseException as e:
        report_failure(ctx, current_phase, e)
        raise


def report_failure(ctx, current_phase, exc):
# Record the tolerated-failure ledger, print the resume hint.
    record_degradations(ctx)
    print(f"\n  [!] Deploy failed during phase {current_phase} of '{ctx.comp_name}'.")
    resume_phase = current_phase
    if current_phase >= 2 and "already exists" in str(exc).lower():
        resume_phase = 1
        print("      This looks like a Proxmox/Terraform state mismatch (something the "
              "prior attempt created still exists, but Terraform's state doesn't know about "
              "it) — resuming from the failed phase would just hit the same error again.")
    print(f"      Resume with: python3 create-competition.py "
          f"--competition {ctx.comp_name} --from-phase {resume_phase} --yes")
