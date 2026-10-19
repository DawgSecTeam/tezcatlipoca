"""deploy(): the sequencer. Takes the lock, prepares, walks PHASES, checkpoints, reports."""

from deploy_lib import phases
from deploy_lib.failure import record_degradations
from deploy_lib.pause import pause_at_boundary
from infra.gates import acquire_deploy_lock
from deploy_lib.prepare import prepare


def deploy(comp_dir, num_teams=None, assume_yes=False, from_phase=1, scoring_vmid=None,
           team_node=None, engine_node=None, force_from_phase=False, nodes_path=None,
           team_identifiers=None, engine_mgmt_ip=None, hold_start=False,
           replant_goldens=False):
# scoring_vmid is persisted to the state document and reused on resume, so concurrent
    # competitions on one node keep their own engine. hold_start likewise lives in the
    # state file (the argument only mints it), so a resume of a held deploy stays held.
    acquire_deploy_lock(comp_dir)
    if replant_goldens:
        from engine.template_freeze import clear_golden_plant_checkpoints
        clear_golden_plant_checkpoints(comp_dir)
        print("  --replant-goldens: golden plant checkpoints cleared")

    ctx = prepare(comp_dir, num_teams=num_teams, assume_yes=assume_yes, from_phase=from_phase,
                  scoring_vmid=scoring_vmid, team_node=team_node, engine_node=engine_node,
                  force_from_phase=force_from_phase, nodes_path=nodes_path,
                  team_identifiers=team_identifiers, engine_mgmt_ip=engine_mgmt_ip)
    if ctx is None:
        return

    if hold_start and not ctx.state.get("hold_start"):
        ctx.state["hold_start"] = True
        ctx.save_state()

    run_pipeline(ctx)

    record_degradations(ctx)
    ctx.save_state()
    phases.finish_deploy(ctx)


def run_pipeline(ctx):
# Any phase failure is recorded (streak + degradations) and re-raised with a resume hint.
    current_phase = max(ctx.from_phase, 1)
    # A run that died while parked leaves its marker in the state file, and the observer
    # reads that marker as the live truth — clear it before it can label a resume paused.
    if ctx.state.pop("paused_after_phase", None) is not None:
        ctx.save_state()
    try:
        for n, phase in enumerate(phases.PHASES, 1):
            # Every phase is CALLED so it can print its own "[N/8] Skipped (resume)"
            # banner, but only a phase that ran may checkpoint or become the failure's
            # phase — checkpointing a skipped one moves the resume guard's answer forward.
            running = ctx.from_phase <= n
            if running:
                current_phase = n
            phase(ctx)
            if running:
                ctx.checkpoint(n)
            if n == 2:
                # Read once, after phase 2's checkpoint: a --from-phase 3+ resume skips the
                # apply but still needs the engine's coordinates. Outside the `running` gate
                # on purpose, so a failure names the phase the operator asked to resume at.
                phases.connect_estate(ctx)
            if running:
                # Last, so a hold lands on a completely done phase; a skipped phase is no
                # boundary and would name a phase this run did not run.
                pause_at_boundary(ctx, n)
    except BaseException as e:
        report_failure(ctx, current_phase, e)
        raise


def report_failure(ctx, current_phase, exc):
    record_degradations(ctx)
    print(f"\n  [!] Deploy failed during phase {current_phase} of '{ctx.comp_name}'.")
    if current_phase >= 2 and "already exists" in str(exc).lower():
        # A deploy never destroys a leftover range, so no --from-phase helps here
        # (docs/usage-agents.md § run ownership).
        print("      Something the prior attempt created still exists and this deploy did not "
              "record it as its own (a half-built clone, an interrupted bridge). A deploy "
              "never destroys a leftover range — reclaim it, then start fresh:")
        print(f"        python3 destroy-competition.py --competition {ctx.comp_name} --yes")
        print(f"        python3 create-competition.py --competition {ctx.comp_name} --yes")
        return
    print(f"      Resume with: python3 create-competition.py "
          f"--competition {ctx.comp_name} --from-phase {current_phase} --yes")
