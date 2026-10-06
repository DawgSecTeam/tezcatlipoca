"""Plant-coverage bookkeeping: which configs a nakon stage failed to land, in state."""


def record_coverage(state, stage_machines, result):
    """Record per-machine unplanted configs in state (M4 plant-coverage source).

    The expectation side is implicit — nakon-config.json is verify's source of truth.
    This records only failures: machine -> [config names whose step reported rc != 0],
    or every config when a machine died before reporting any step.

    The golden plant (phase 4) records through the same shape via build_golden_set's
    coverage callback, under '{box}-golden' keys (satellite slots: '{box}-golden-slot{N}')
    that verify maps onto every team copy of the box: a strict-mode failure aborts the
    deploy before anything is recorded, but an alpine_services-tolerated failure is a
    real gap on the golden disk and must reach the gate. A machine whose stage reported
    NO failed steps drops its stale entry: a recovered config (replanted by a later
    sweep) must not keep the coverage gate red forever (amongus-cde-2026 2026-09-30:
    SMB v1 stayed 'failed' across three green replants)."""
    if result is None or not getattr(result, "machines", None):
        return  # no --json outcome (older nakon) — coverage falls back to the tally
    failed = result.failed_configs()
    cov = state.setdefault("plant_coverage_failed", {})
    if not failed:
        for m in stage_machines:
            cov.pop(m["name"], None)
        return
    for m in stage_machines:
        bad = failed.get(m["name"])
        if not bad:
            cov.pop(m["name"], None)
            continue
        if bad == {"<machine failed before any step>"}:
            bad = {(c if isinstance(c, str) else c["name"]) for c in m["configurations"]}
        cov[m["name"]] = sorted(set(cov.get(m["name"]) or []) | set(bad))


def record_stage_coverage(state, stage_machines, result, save_state):
    """Record one post-clone stage's coverage AND persist it, unconditionally.

    record_coverage's stale-entry clearing only counts once it reaches disk. Phase 6
    used to guard its save on the nakon tally (`if state["nakon_failed_steps"]`), so a
    final pass that was fully green yet popped a stale entry lost the pop at process
    exit and verify's coverage gate stayed red — re-creating exactly the bug ff9b19f
    fixed (amongus-cde-2026 2026-09-30: "SMB v1 stayed 'failed' across three green
    replants"). The window is narrow but real: phase-5 tally empty, final pass fully
    green, and the stale entry belongs to a final-only machine (one carrying just
    systemd-system-masked / hosts-redirect-linux, so repair_machines never names it).
    save_state is injected so the always-persist invariant is testable without a real
    state file."""
    record_coverage(state, stage_machines, result)
    save_state()


def record_clean_coverage(ctx):
    """Record the "nothing to plant here" verdict for a post-clone stage.

    A stage with no configurations is a clean result, not an absent one: without the
    keys verify's coverage gate fails closed with "coverage was never recorded"
    (live-found 2026-10-02). Shared by phase 6 (no repair-stage configs) and phase 7
    (no final-stage configs)."""
    ctx.state.setdefault("plant_coverage_failed", {})
    ctx.state.setdefault("nakon_failed_steps", [])
    ctx.save_state()
