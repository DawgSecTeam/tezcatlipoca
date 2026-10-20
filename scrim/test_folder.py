import json
from pathlib import Path

from artifacts_lib import artifacts_ops
from common.comp_paths import resolve_comp_dir
from scrim import core
from scrim import procs
from scrim import red_link
from scrim.core import log
from estate import config


BOXES_JSON = "boxes.json"


def _box_names(comp):
    """[] when unreadable; snapshotted into test.json because a later deploy may rewrite the comp dir."""
    try:
        boxes = json.loads((Path(comp) / BOXES_JSON).read_text())
    except (OSError, ValueError):
        return []
    return [b.get("name") for b in boxes if isinstance(b, dict) and b.get("name")]


def resolve_run_dir(comp, args):
    """The default run dir IS the test folder; on resume the recorded `paths.run_dir` is authoritative — never mints a second key."""
    test_path, manifest = artifacts_ops.ensure_test(
        comp, kind="scrim", script="run-agent-scrim.py",
        # fresh keys only: a resume lands in the folder its first attempt created (`recorded` below)
        key=(None if (getattr(args, "run_dir", None)
                      or getattr(args, "resume_event", False))
             else artifacts_ops.next_run_key(comp)),
        teams=getattr(args, "teams", None), boxes=_box_names(comp),
        node=config.active().proxmox.node,
        endpoint=config.active().proxmox.endpoint)
    args.test_dir = str(test_path)
    explicit = getattr(args, "run_dir", None)
    recorded = (manifest.get("paths") or {}).get("run_dir")
    run_dir = Path(explicit or (recorded if getattr(args, "resume_event", False) else None)
                   or test_path)
    artifacts_ops.record_paths(test_path, run_dir=str(run_dir))
    return run_dir, test_path


def adopt_run_key(args, comp):
    """A fresh run mints `untagged-<stamp>` before the deploy has a run id, and the folder kept
    that name through the report — the seal command and teardown then disagreed about which
    folder is the run's (live: run-9128c13a, reported under untagged-20261020-091614)."""
    run_id = artifacts_ops.run_id_from_state(Path(comp))
    current = Path(getattr(args, "test_dir", "") or "")
    if not run_id or not current.is_dir() or not current.name.startswith("untagged"):
        return None
    target = current.parent / run_id
    if target.exists():
        return None
    current.rename(target)
    manifest = artifacts_ops.load_manifest(target)
    manifest["key"] = run_id
    manifest["run_id"] = manifest.get("run_id") or run_id
    if (manifest.get("paths") or {}).get("run_dir") in (str(current),
                                                         str(current.resolve())):
        manifest["paths"]["run_dir"] = str(target)
    artifacts_ops.save_manifest(target, manifest)
    args.test_dir = str(target)
    log(f"test folder follows the run: {target.name}")
    return target


def record_agent(args, side, **fields):
    """Merged, never replaced: two stages record into one map and a resume must not erase the other side."""
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        return {}
    manifest = artifacts_ops.load_manifest(test_dir)
    agents = manifest.get("agents") or {}
    agents[side] = {**(agents.get(side) or {}), **fields}
    artifacts_ops.update_manifest(test_dir, agents=agents)
    return agents[side]


def record_red_agent(args):
    """`ssh` is the same spec the collector dials with, so the two cannot diverge."""
    spec, _common = red_link._red_ssh_spec(args)
    node = config.active().proxmox.node
    return record_agent(args, "red", present=True, ip=spec["host"],
                        vmid=getattr(args, "red_vmid", None) or None, ssh=spec,
                        **({"node": node} if node else {}))


def record_blue_agents(args, run_dir):
    """Blue runs operator-side, so the collector needs its workdir paths, not just "present"."""
    record_agent(args, "blue", present=True, teams=getattr(args, "teams", None))
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        return
    artifacts_ops.record_paths(
        test_dir,
        blue_workdirs=[str(Path(run_dir) / f"blue-team{n}")
                       for n in range(1, (getattr(args, "teams", 0) or 0) + 1)],
        engine_evidence=str(Path(run_dir) / "evidence" / "engine"))


def collect_run_artifacts(args):
    """Never raises (a dead guest cannot hold the destroy hostage); must run before the red01 destroy."""
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        log("WARNING: no test folder on this run — artifact collection skipped")
        return None
    comp = resolve_comp_dir(args.competition, getattr(args, "comp_dir", None))
    try:
        collection = artifacts_ops.collect(
            test_dir,
            artifacts_ops.plan_targets(artifacts_ops.load_manifest(test_dir), comp_dir=comp))
    except Exception as e:
        log(f"WARNING: artifact collection failed: {e}")
        return None
    summary = (collection or {}).get("summary") or {}
    log("artifacts collected: " + ", ".join(f"{k}={v}" for k, v in sorted(summary.items())))
    return collection


def write_interaction_report(args):
    """Both halves warn and continue: reporting must never block the destroy."""
    # the test_dir fallback covers callers that never set run_dir (default: run dir == test folder)
    run_dir = Path(getattr(args, "run_dir", None) or getattr(args, "test_dir", None) or ".")
    try:
        r = procs.run([core.local_python(), "scrim-report.py", str(run_dir)], cwd=core.REPO, timeout=300,
                      check=False)
        if r.returncode != 0:
            log(f"WARNING: scrim-report.py exited {r.returncode} — "
                f"{run_dir / 'INTERACTION.md'} may be stale")
    except Exception as e:
        log(f"WARNING: scrim-report.py failed: {e}")
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        return {}
    # collection ran before the report existed — seal INTERACTION.md where the collector would have put it
    interaction = run_dir / "INTERACTION.md"
    if interaction.exists():
        try:
            artifacts_ops.seal_local_file(
                interaction, Path(test_dir) / "evidence" / "harness" / "INTERACTION.md")
        except OSError as e:
            log(f"WARNING: could not file INTERACTION.md into the test folder: {e}")
    try:
        return artifacts_ops.ingest_verdict(test_dir)
    except Exception as e:
        log(f"WARNING: could not ingest the scrim verdict: {e}")
        return {}
