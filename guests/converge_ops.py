"""The one nakon stage-runner and failure ledger every plant path shares.

Deploy phases 4/6/7 and every redeploy mode used to carry their own copy of "read the stage
file, build the bundle, filter to the machines we mean, run nakon, merge the failures into
state". Four copies drifted (one overwrote the tally, one never recorded coverage), so the
sequence lives here once and the callers only pick the stage and the machines.
"""

import json
from pathlib import Path

from common.constants import PER_MACHINE_NAKON_BUDGET
from common.timing import timed
from nakonops.nakon_bundle_ops import build_nakon_bundle
from nakonops.nakon_run_ops import run_nakon

MAX_RECORDED_FAILURES = 40


def stage_machines(path):
    return json.loads(Path(path).read_text())["machines"]


def run_stage(path, machines, *, ssh_key, scoring_user, scoring_ip, jobs,
              strict=False, label=None, only=None, comp_dir=None, timeout=None,
              bundle=None):
    """Run one nakon stage for `machines`; `only` narrows to those machine names.

    `bundle` is the content-addressed bundle when the caller already built it — builds are
    operator-side and need the vulndb, so nothing should build one per pass by accident.
    """
    if only is not None:
        machines = [m for m in machines if m["name"] in set(only)]
    if not machines:
        return None
    budget = timeout or max(2400, PER_MACHINE_NAKON_BUDGET * len(machines))
    stage = label or Path(path).stem.lstrip(".")
    bundle = bundle or build_nakon_bundle(path)
    with timed(comp_dir, stage, "nakon", f"x{len(machines)}") if comp_dir else _null():
        return run_nakon(ssh_key, scoring_user, scoring_ip, bundle, path,
                         only=[m["name"] for m in machines], timeout=budget,
                         strict=strict, jobs=jobs)


def merge_failures(state, result, stage):
    """Append (never overwrite) this stage's failures, deduped and capped."""
    if not result or not result.failed:
        return
    merged = list(state.get("nakon_failed_steps") or [])
    merged += [f"{stage}: {line}" for line in result.failed]
    seen = set()
    state["nakon_failed_steps"] = [x for x in merged if not (x in seen or seen.add(x))][
        :MAX_RECORDED_FAILURES]


def record_coverage(state, stage_machines, result):
    """Which configs a stage failed to land, keyed by machine name (verify's gate reads it)."""
    failed = result.failed_configs()
    cov = state.setdefault("plant_coverage_failed", {})
    if not failed:
        for machine in stage_machines:
            cov.pop(machine["name"], None)  # a recovered config must not keep the gate red
        return
    for machine in stage_machines:
        bad = failed.get(machine["name"])
        if not bad:
            cov.pop(machine["name"], None)
            continue
        if bad == {"<machine failed before any step>"}:
            bad = {(c if isinstance(c, str) else c["name"])
                   for c in machine["configurations"]}
        cov[machine["name"]] = sorted(set(cov.get(machine["name"]) or []) | set(bad))


def record_stage(state, machines, result, save_state, stage):
    """Merge the tally and re-record plant coverage; always saves.

    A green pass must still save: recording coverage may have CLEARED a stale failure, and an
    unsaved clear leaves verify's coverage gate red.
    """
    merge_failures(state, result, stage)
    record_coverage(state, machines, result)
    save_state()


def record_clean_coverage(ctx):
    """A stage with nothing to plant is clean, not absent: the gate fails closed on no record."""
    ctx.state.setdefault("plant_coverage_failed", {})
    ctx.state.setdefault("nakon_failed_steps", [])
    ctx.save_state()


class _null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
