"""Orchestration: finalize a folder, and teardown's one-call artifact step."""

from pathlib import Path

from .archive import archive_test
from .collect import collect, load_collection
from .constants import FAILED, REPO, SIDES, SKIPPED
from .env import in_worktree, iso
from .index import update_index
from .manifest import ensure_test, load_manifest, record_paths, update_manifest
from .paths import latest_run_key, test_dir, test_key
from .plan import plan_targets
from .report import write_report_skeleton, write_stub
from .status import warn_summary, why_missing
from .verdict import ensure_verdict


def finalize(test_path, *, manifest=None):
    test_path = Path(test_path)
    manifest = manifest or load_manifest(test_path)
    collection = load_collection(test_path)
    agents = manifest.get("agents") or {}
    for side in ("red", "blue"):
        filename = SIDES[side]
        if (test_path / filename).exists():
            continue
        if any(d.get("path") == filename for d in collection.get("derived", [])):
            continue
        if not (agents.get(side) or {}).get("present"):
            write_stub(test_path, side, SKIPPED, f"no {side} agent in this run", manifest=manifest)
            continue
        status, reason = why_missing(collection, side)
        write_stub(test_path, side, status, reason, manifest=manifest)
    report = write_report_skeleton(test_path)
    update_index(Path(manifest.get("comp_dir") or test_path.parent.parent))
    return {"report": str(report),
            "warnings": warn_summary(collection, manifest, test_path)}


# The staging scrim/red_link.pull_red_state does for the harness: /var/lib/tlaloc is 0600 root
# and the collector scp's as the box user, so an unstaged target records absent — and for a dead
# harness this collection is the only copy of what red planted.
_STAGE_CMD = ("sudo -n mkdir -p /tmp/ba && sudo -n chmod 755 /tmp/ba; "
              "for f in world.json events.jsonl report.md report-secrets.md "
              "restore-table.json; do "
              "sudo -n cp /var/lib/tlaloc/$f /tmp/ba/$f 2>/dev/null; done; "
              "sudo -n cp /var/lib/tlaloc/intel/red/manifest.json /tmp/ba/manifest.json "
              "2>/dev/null; "
              "sudo -n chmod 644 /tmp/ba/* 2>/dev/null; true")


def stage_red_artifacts(manifest, timeout=75):
# Returns True when the attempt was made, not when it worked. Never raises: a dead red01
# must record as unreachable, not hold up a teardown.
    import shutil
    import subprocess

    red = ((manifest.get("agents") or {}).get("red") or {})
    spec = red.get("ssh") or {}
    host = spec.get("host") or red.get("ip")
    if not host:
        return False
    argv = [shutil.which("ssh") or "ssh",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=15", "-i", spec.get("key") or str(REPO / "proxmox")]
    if spec.get("jump"):
        argv += ["-o", spec["jump"]]
    argv += [f"{spec.get('user') or 'sysadmin'}@{host}", _STAGE_CMD]
    try:
        subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception:                                        # noqa: BLE001 - best effort
        return False
    return True


# The harness mints a per-run folder, so the folder to collect is the newest one belonging to
# this deploy run id — not the base key. Shared with the teardown steps that write into it.
def test_folder_for_teardown(comp_dir, *, run_id=None, teams=None, boxes=None, node=None,
                             nodes=None, script="destroy-competition.py"):
    comp_dir = Path(comp_dir)
    key, run_id = latest_run_key(comp_dir, run_id)
    prior = load_manifest(test_dir(comp_dir, key))
    return ensure_test(comp_dir, kind=prior.get("kind") or "deploy", run_id=run_id, key=key,
                       script=script,
                       teams=len(teams) if hasattr(teams, "__len__") else teams,
                       boxes=[b.get("name") for b in boxes] if boxes else None,
                       node=node, nodes=nodes)


def collect_for_teardown(comp_dir, *, run_id=None, teams=None, boxes=None, node=None,
                         nodes=None, script="destroy-competition.py", transport=None,
                         dry_run=False, timeout=45, echo=print, stage_red=None):
# Teardown's whole artifact step: the one call destroy-competition.py makes. Ordering is the
#     entire point — it runs after the confirmation prompt and before the first destructive call,
#     because pre_stop_windows_boxes force-stops every clone and a stopped guest's agent can no
#     longer answer. Warns and proceeds: a dead box must not wedge a teardown, and the failure is
#     recorded in collection.json and REPORT.md. Red01 is destroyed by the teardown's own red step,
#     so this is the safety net for a dead harness, not the primary path.
    comp_dir = Path(comp_dir)
    if transport is None:
        # Resolved through the public facade at call time so callers that patch
        # `artifacts_ops.default_transport` steer the teardown collector.
        from artifacts_lib import artifacts_ops
        transport = artifacts_ops.default_transport()
    path, manifest = test_folder_for_teardown(
        comp_dir, run_id=run_id, teams=teams, boxes=boxes, node=node, nodes=nodes, script=script)
    if not (manifest.get("paths") or {}).get("run_dir"):
        # The harness's run dir, for a harness that died before recording it: the test folder
        # itself, where run-agent-scrim.py writes by default.
        if (path / "run.json").exists() or (path / "T0.txt").exists():
            record_paths(path, run_dir=str(path))
    update_manifest(path, teardown={"at": iso(), "by": script, "dry_run": bool(dry_run),
                                   "collector_timeout_s": timeout})
    manifest = load_manifest(path)
    # Injectable like `transport`: the offline lifecycle tests must not reach the network, and a
    # fixture host would wait out a real connect timeout.
    (stage_red or stage_red_artifacts)(manifest)
    collection = collect(path, plan_targets(manifest, comp_dir=comp_dir), transport=transport,
                         dry_run=dry_run, timeout=timeout)
    if not dry_run and any(f.get("status") == FAILED
                           for t in collection.get("targets", []) for f in t.get("files", [])):
        # A failed transfer is transient by definition (the source answered): one retry, before
        # the destroy makes a second attempt impossible. `unreachable` is not retried.
        echo("  Retrying the transfers that failed...")
        collection = collect(path, plan_targets(manifest, comp_dir=comp_dir),
                             transport=transport, timeout=timeout)
    # The verdict and gate table come from scrim-report.py, which reads only the run dir (no
    # network, no API). The harness normally runs it; teardown runs it for a dead harness —
    # otherwise a collected INTERACTION.md nobody parsed leaves REPORT.md verdict-less.
    verdict = ensure_verdict(path, echo=echo, dry_run=dry_run)
    result = finalize(path, manifest=load_manifest(path))
    summary = collection.get("summary", {})
    echo(f"  Artifacts  : {path}")
    echo("  Collected  : " + ", ".join(f"{k}={v}" for k, v in sorted(summary.items())
                                       if k != "files") + f" (files={summary.get('files', 0)})")
    if verdict:
        echo(f"  Verdict    : {verdict['status']}"
             + (f" — interaction score {verdict['score']}" if verdict.get("score") is not None
                else "") + f" ({verdict.get('gates_passed')} pass / "
             f"{verdict.get('gates_failed')} fail / {verdict.get('gates_na')} n/a)")
    for warning in result["warnings"]:
        echo(f"  WARNING: {warning}")
    if not result["warnings"]:
        echo("  No declared document is missing (any `absent` item is recorded in "
             "collection.json).")
    if in_worktree() and not dry_run:
        try:
            echo(f"  Archived   : {archive_test(comp_dir, path.name)}")
        except (OSError, RuntimeError) as e:
            echo(f"  WARNING: could not archive this test folder outside the worktree: {e}")
    return collection
