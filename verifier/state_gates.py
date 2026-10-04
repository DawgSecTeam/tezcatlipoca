"""Gates fed by .deploy_state.json: tolerated-failure ledger and plant coverage."""

import json

from verifier.loaders import read_deploy_state
from verifier.model import gate_fail, gate_pass, gate_skip


def check_degradations(comp_dir):
    """Surface the prerequisites that failed while the deploy continued anyway.

    The deploy tolerates some failures on purpose (each site has its own reason), but
    it records every one of them in `.deploy_state.json["degradations"]` — apt prep that
    installed nothing, an auth ladder that failed on both transports, a box that never
    settled, a node scan that never happened. Without this line those exist only as
    WARNING text in a scrollback nobody reads, which is how a range can come up "green"
    with every service down. Warning-level, not gating: the deploy did survive them, and
    the plant-coverage/service gates are what decide whether the range actually works.
    """
    state = read_deploy_state(comp_dir)
    if state is None:
        return gate_skip("degradations", "no readable .deploy_state.json", gating=False)
    entries = state.get("degradations")
    if entries is None:
        print("  SKIP  — this deploy recorded no degradation ledger "
              "(pre-dates the ledger, or state was rewritten).")
        return gate_skip("degradations", "not recorded by this deploy", gating=False)
    if not entries:
        print("  PASS  — no tolerated failures recorded.")
        return gate_pass("degradations", "none recorded")
    seen = {}
    for entry in entries:
        seen[entry.get("what", "?")] = seen.get(entry.get("what", "?"), 0) + 1
    detail = ", ".join(f"{what} x{count}" if count > 1 else what
                       for what, count in sorted(seen.items()))
    for entry in entries:
        print(f"  WARN  — {entry.get('what')}: {str(entry.get('detail', ''))[:110]}")
    print(f"  WARN  — {len(entries)} tolerated failure(s) recorded; see "
          f".deploy_state.json['degradations']")
    return gate_pass("degradations", f"{len(entries)} tolerated: {detail[:150]}")


def check_plant_coverage(comp_dir):
    """M4 plant-coverage gate: every machine's FULL expected configuration list
    (nakon-config.json) must have actually planted.

    Deploy records failures per machine in .deploy_state.json["plant_coverage_failed"]
    (machine -> [config names whose nakon step reported rc != 0], or every config when
    a machine died before reporting any step). Golden-stage entries map onto every team
    copy of that box (a golden failure means the clones inherited the gap): the golden
    plant records under '{box}-golden' (phase 4, via build_golden_set's coverage
    callback — including alpine_services-tolerated failures), and satellite slots
    record '{box}-golden-slot{N}' since every slot's stage config names its machine
    identically; any slot's failure flags every team copy. This is the backstop that
    catches a broken/undeclared-var config the moment it fails to plant, instead of a
    mid-competition discovery.

    D2 (live-found 2026-10-02): this gate used to read ONLY plant_coverage_failed and
    fail OPEN when it was absent — missing/unparseable state, an older state, or a
    nakon that produced no --json outcome left `failed = {}`, so it printed
    "PASS all N machine(s) report full config coverage" and exited 0 while its own
    SUMMARY said "plant integrity: WARNING — last nakon plant recorded N FAILED
    step(s)". The whole premise of verify (docs/known-issues.md: nakon failures are
    silent by design) was vacuous in exactly that case. It now fails CLOSED: the
    nakon tally deploy.py:105 promises as the fallback is consulted, a non-empty tally
    can never PASS, and when neither source exists the gate FAILs with "coverage was
    never recorded".

    Returns a GateResult (never a bare tuple — the old (checked, ok) arity was part of
    the D1 unpacking crash family)."""
    state = read_deploy_state(comp_dir) or {}
    # `None` distinguishes "never recorded" from "recorded and clean ({})" — the two
    # must not be conflated, which is what made the old gate fail open.
    failed = state.get("plant_coverage_failed")
    tally = state.get("nakon_failed_steps")
    config_path = comp_dir / "nakon-config.json"
    if not config_path.exists():
        print("  SKIP  — no nakon-config.json (nothing expected).")
        return gate_skip("plant_coverage", "no nakon-config.json", gating=False)
    try:
        machines = json.loads(config_path.read_text())["machines"]
    except (OSError, ValueError, KeyError) as e:
        print(f"  FAIL  — cannot read nakon-config.json: {e}")
        return gate_fail("plant_coverage", f"cannot read nakon-config.json ({str(e)[:60]})")

    def cfg_name(c):
        return c if isinstance(c, str) else c["name"]

    recorded = failed if isinstance(failed, dict) else None
    unplanted = {}
    for m in machines:
        name = m.get("name", "?")
        expected = {cfg_name(c) for c in m.get("configurations", [])}
        machine_bad = [c for c in (recorded or {}).get(name) or []]
        # Golden-stage keys: slot 0 records '{box}-golden', satellite slot N records
        # '{box}-golden-slot{N}' (every slot's stage config names its golden machine
        # identically, so the keys must not collide across slots). A failure on ANY
        # slot's golden flags every team copy of the box — each clone inherits its
        # own slot's disk, and a gap on any of them is a range-wide problem.
        base = name.rsplit("-team", 1)[0]
        golden_bad = [c for k, v in (recorded or {}).items()
                      if k == f"{base}-golden" or k.startswith(f"{base}-golden-slot")
                      for c in (v or [])]
        # Intersect each recorded failure with what this machine STILL expects: a
        # failure for a config no longer in its `configurations` is stale and must not
        # fail the gate (amongus-cde-2026 2026-09-30: a recovered SMB v1 entry stayed
        # 'failed' across three green replants). The `<machine ...>` sentinel is kept
        # because it means "died before reporting any step" and is never a config name.
        bad = {c for c in machine_bad if c in expected or c.startswith("<machine")}
        # A golden-stage failure means every team copy inherited the gap.
        bad |= {f"{c} (golden-stage)" for c in golden_bad if c in expected}
        if bad:
            unplanted[name] = sorted(bad)

    problems = []
    if unplanted:
        for name, cfgs in sorted(unplanted.items()):
            print(f"  FAIL  {name}: not planted: {', '.join(cfgs)}")
        problems.append(f"{sum(len(c) for c in unplanted.values())} unplanted config(s)")
    if tally and recorded is None:
        # The tally GATES only when it is the fallback (no structured record — older
        # nakon). When a record exists it is the authority: the tally is merged
        # history ("repair: X FAILED" stays listed even after a later stage replanted
        # X green, scrim-one 2026-10-03), and gating on it means a recovered failure
        # keeps the gate red forever — the tally twin of the stale-record bug the
        # record side already fixed.
        print(f"  FAIL  nakon recorded {len(tally)} FAILED plant step(s): "
              f"{', '.join(str(s)[:80] for s in tally[:3])}"
              f"{' …' if len(tally) > 3 else ''}")
        problems.append(f"{len(tally)} failed nakon step(s)")
    elif tally:
        print(f"  (informational) nakon history: {len(tally)} FAILED plant step(s) on "
              f"record — superseded failures stay listed; the coverage record above is "
              f"authoritative")
    if problems:
        return gate_fail("plant_coverage", "; ".join(problems))

    if recorded is None:
        if tally is None:
            print("  FAIL  — plant coverage was never recorded: no "
                  "'plant_coverage_failed' in .deploy_state.json and no nakon FAILED "
                  "tally to fall back on (pre-tally deploy?). An unrecorded coverage "
                  "gate is not a passing one.")
            return gate_fail("plant_coverage", "coverage was never recorded")
        # deploy.py's documented fallback: "no --json outcome (older nakon) —
        # coverage falls back to the tally", and the tally here is present and clean.
        print(f"  PASS  no coverage record (older nakon --json), so coverage falls back "
              f"to the tally: 0 FAILED steps for {len(machines)} machine(s)")
        return gate_pass("plant_coverage", f"tally clean (no coverage record; {len(machines)} machines)")
    print(f"  PASS  all {len(machines)} machine(s) report full config coverage")
    return gate_pass("plant_coverage", f"all {len(machines)} machine(s)")
