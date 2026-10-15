"""State gates fed by the competition's state document: plant coverage + the tolerated-failure ledger."""

import json

from verifier.loaders import read_deploy_state
from verifier.model import gate_fail, gate_pass, gate_skip


def check_plant_coverage(comp_dir):
# M4 plant-coverage gate: every machine's FULL expected configuration list
#     (nakon-config.json) must have actually planted.
#
#     Deploy records failures per machine in the state document's "plant_coverage_failed"
#     (machine -> [config names whose nakon step reported rc != 0], or every config when
#     a machine died before reporting any step). Golden-stage entries map onto every team
#     copy of that box (a golden failure means the clones inherited the gap): the golden
#     plant records under '{box}-golden' (phase 4, via build_golden_set's coverage
#     callback — including alpine_services-tolerated failures), and satellite slots
#     record '{box}-golden-slot{N}' since every slot's stage config names its machine
#     identically; any slot's failure flags every team copy. This is the backstop that
#     catches a broken/undeclared-var config the moment it fails to plant, instead of a
#     mid-competition discovery.
#
#     D2 (live-found 2026-10-02): this gate used to read ONLY plant_coverage_failed and
#     fail OPEN when it was absent — missing/unparseable state, an older state, or a
#     nakon that produced no --json outcome left `failed = {}`, so it printed
#     "PASS all N machine(s) report full config coverage" and exited 0 while its own
#     SUMMARY said "plant integrity: WARNING — last nakon plant recorded N FAILED
#     step(s)". The whole premise of verify (docs/known-issues.md: nakon failures are
#     silent by design) was vacuous in exactly that case. It now fails CLOSED: the
#     nakon tally is never a substitute for the record: a missing record FAILs with
#     "coverage was never recorded".
#
#     Returns a GateResult (never a bare tuple — the old (checked, ok) arity was part of
#     the D1 unpacking crash family).
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

    recorded = failed if isinstance(failed, dict) else None
    if recorded is None:
        print("  FAIL  — plant coverage was never recorded: no 'plant_coverage_failed' in "
              "the state document. An unrecorded coverage gate is not a passing one.")
        return gate_fail("plant_coverage", "coverage was never recorded")
    unplanted = {}
    for m in machines:
        name = m.get("name", "?")
        expected = {(c if isinstance(c, str) else c["name"]) for c in m.get("configurations", [])}
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
    if tally:
        # The tally is merged history, never the authority: "predomain: X FAILED" stays listed
        # even after a later stage replanted X green (scrim-one 2026-10-03).
        print(f"  (informational) nakon history: {len(tally)} FAILED plant step(s) on "
              f"record — superseded failures stay listed; the coverage record above is "
              f"authoritative")
    if problems:
        return gate_fail("plant_coverage", "; ".join(problems))

    print(f"  PASS  all {len(machines)} machine(s) report full config coverage")
    return gate_pass("plant_coverage", f"all {len(machines)} machine(s)")


def check_degradations(comp_dir):
# Tolerated-failure gate: every entry in the state document's "degradations" is a
#     prerequisite that failed while the deploy continued — recorded by
#     common.utils.record_degradation and persisted by deploy_lib.failure on both the
#     success and failure paths.
#
#     The ledger exists so "the range came up with every service down" (or with no red
#     inside it at all) has a named cause instead of a scrollback WARNING; both the
#     recorder's docstring and the deploy's summary line promise verify surfaces it, and
#     until this gate existed nothing read the key. A degraded range is not a passing one:
#     an assume-breach red that never landed, a box that never answered, an unplanted
#     config the coverage gate cannot see — each is a failure of the run's own purpose.
#
#     Fails CLOSED on an absent key, like the coverage gate: a state written before the
#     ledger existed is not evidence of a clean run.
    state = read_deploy_state(comp_dir) or {}
    entries = state.get("degradations")
    if not isinstance(entries, list):
        print("  FAIL  — the tolerated-failure ledger was never recorded: no "
              "'degradations' in the state document. An unrecorded ledger is not a "
              "passing one.")
        return gate_fail("degradations", "degradations were never recorded")
    if entries:
        for entry in entries:
            entry = entry if isinstance(entry, dict) else {}
            print(f"  FAIL  {entry.get('what') or '?'}: "
                  f"{str(entry.get('detail') or '')[:160]}")
        return gate_fail("degradations",
                         f"{len(entries)} tolerated failure(s) while the deploy continued")
    print("  PASS  the deploy recorded no tolerated failures")
    return gate_pass("degradations", "none recorded")
