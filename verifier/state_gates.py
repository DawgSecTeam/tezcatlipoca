"""State gates fed by the competition's state document: plant coverage + the tolerated-failure ledger."""

import json

from verifier.loaders import read_deploy_state
from verifier.model import gate_fail, gate_pass, gate_skip


def check_plant_coverage(comp_dir):
# Every machine's FULL expected configuration list must have planted. Golden-stage failures flag
#     every team copy of that box; a missing record FAILs closed ("coverage was never recorded").
    state = read_deploy_state(comp_dir) or {}
    # `None` distinguishes "never recorded" from "recorded and clean ({})" — the two must not be conflated.
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
        # Golden-stage keys: slot 0 records '{box}-golden', satellite slot N '{box}-golden-slot{N}';
        # a failure on ANY slot flags every team copy of the box.
        base = name.rsplit("-team", 1)[0]
        golden_bad = [c for k, v in (recorded or {}).items()
                      if k == f"{base}-golden" or k.startswith(f"{base}-golden-slot")
                      for c in (v or [])]
        # Intersect each recorded failure with what this machine STILL expects: a failure for a
        # config no longer listed is stale. The `<machine ...>` sentinel means "died before
        # reporting any step" and is never a config name.
        bad = {c for c in machine_bad if c in expected or c.startswith("<machine")}
        bad |= {f"{c} (golden-stage)" for c in golden_bad if c in expected}
        if bad:
            unplanted[name] = sorted(bad)

    problems = []
    if unplanted:
        for name, cfgs in sorted(unplanted.items()):
            print(f"  FAIL  {name}: not planted: {', '.join(cfgs)}")
        problems.append(f"{sum(len(c) for c in unplanted.values())} unplanted config(s)")
    if tally:
        # The tally is merged history, never the authority: a superseded failure stays listed.
        print(f"  (informational) nakon history: {len(tally)} FAILED plant step(s) on "
              f"record — superseded failures stay listed; the coverage record above is "
              f"authoritative")
    if problems:
        return gate_fail("plant_coverage", "; ".join(problems))

    print(f"  PASS  all {len(machines)} machine(s) report full config coverage")
    return gate_pass("plant_coverage", f"all {len(machines)} machine(s)")


def check_degradations(comp_dir):
# Every entry in the state document's "degradations" is a prerequisite that failed while the deploy
#     continued; fails CLOSED on an absent key (a state predating the ledger is not a clean run).
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
