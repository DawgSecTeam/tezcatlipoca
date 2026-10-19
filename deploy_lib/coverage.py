"""Plant-coverage bookkeeping: which configs a nakon stage failed to land, in state."""


def record_coverage(state, stage_machines, result):
    # Records failures only (verify's expectation side is nakon-config.json).
    # Golden-plant failures land under '{box}-golden' keys verify maps to team copies.
    failed = result.failed_configs()
    cov = state.setdefault("plant_coverage_failed", {})
    if not failed:
        for m in stage_machines:
            cov.pop(m["name"], None)  # a recovered config must not keep the gate red
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
    # Always save: a green pass's stale-entry pop is lost if persistence rides on
    # the caller's "tally non-empty" condition, and verify's gate stays red.
    record_coverage(state, stage_machines, result)
    save_state()


def record_clean_coverage(ctx):
    # A stage with nothing to plant is clean, not absent: without these keys
    # verify's coverage gate fails closed ("coverage was never recorded").
    ctx.state.setdefault("plant_coverage_failed", {})
    ctx.state.setdefault("nakon_failed_steps", [])
    ctx.save_state()
