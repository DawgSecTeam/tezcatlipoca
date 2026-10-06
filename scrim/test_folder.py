import json
import os
from pathlib import Path

import artifacts_ops
from scrim import core
from scrim import procs
from scrim import red_link
from scrim.core import log


BOXES_JSON = "boxes.json"


def _box_names(comp):
    """Box names from the comp's boxes.json; [] when it is absent or unreadable.

    Recorded in test.json so a teardown-time reader knows what the run covered without
    re-reading a comp dir a later deploy may have rewritten."""
    try:
        boxes = json.loads((Path(comp) / BOXES_JSON).read_text())
    except (OSError, ValueError):
        return []
    return [b.get("name") for b in boxes if isinstance(b, dict) and b.get("name")]


def resolve_run_dir(comp, args):
    """Open this run's test folder and return `(run_dir, test_dir)`.

    The default run dir IS the test folder — `competitions/<comp>/.automated-tests/<key>`
    with `key = artifacts_ops.test_key(comp)` (the run id when the deploy has one) — so the
    harness writes run.json/T0.txt/blue-team*/evidence straight into the folder the
    collector and the reports live in, instead of a post-hoc move out of a comp-keyed
    `scrim-runs/<comp>` dir. `--run-dir <path>` remains the debugging escape hatch: the run
    writes there and the test folder records `paths.run_dir` so the collector still finds
    the evidence.

    Resolution order, which is also the resume rule: an explicit `--run-dir` wins; else a
    `paths.run_dir` already recorded in test.json is authoritative; else the test folder.
    Both main() branches call this, so a fresh run and its `--resume-event` land in one
    folder — a resume never mints a second key or forks the evidence.
    """
    test_path, manifest = artifacts_ops.ensure_test(
        comp, kind="scrim", script="run-agent-scrim.py",
        teams=getattr(args, "teams", None), boxes=_box_names(comp),
        node=os.environ.get("TF_VAR_proxmox_node"),
        endpoint=os.environ.get("TF_VAR_proxmox_endpoint"))
    args.test_dir = str(test_path)
    explicit = getattr(args, "run_dir", None)
    recorded = (manifest.get("paths") or {}).get("run_dir")
    run_dir = Path(explicit or recorded or test_path)
    artifacts_ops.record_paths(test_path, run_dir=str(run_dir))
    return run_dir, test_path


def record_agent(args, side, **fields):
    """Merge one agent side's identity into test.json's `agents` map; return that side.

    Merged, never replaced: red is recorded in stage_red and blue in stage_blues, and a
    resume re-recording one side must not erase what the other side, or an earlier call,
    already knew (INV9)."""
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        return {}
    manifest = artifacts_ops.load_manifest(test_dir)
    agents = manifest.get("agents") or {}
    agents[side] = {**(agents.get(side) or {}), **fields}
    artifacts_ops.update_manifest(test_dir, agents=agents)
    return agents[side]


def record_red_agent(args):
    """Write red01's identity, address and ssh route into test.json (INV5).

    Recorded in stage_red, where the values are first known, because nothing later can
    recover them honestly: bad-auto's config.yaml is a rewritten singleton, so a
    teardown-time reader of it can point at ANOTHER run's red01. `ssh` comes from the same
    helper `_red_ssh_ctx` builds its scp/ssh arguments from, so the harness and the
    collector cannot dial different addresses."""
    spec, _common = red_link._red_ssh_spec(args)
    node = os.environ.get("TF_VAR_proxmox_node")
    return record_agent(args, "red", present=True, ip=spec["host"],
                        vmid=getattr(args, "red_vmid", None) or None, ssh=spec,
                        **({"node": node} if node else {}))


def record_blue_agents(args, run_dir):
    """Blue's identity and the paths its deliverables live at (INV5).

    Blue never runs on a guest box — its workdirs are operator-side — so the collector
    needs the paths, not just "present": a teardown that has to guess workdir names
    collects nothing. `engine_evidence` is recorded here too; stage_capture creates it."""
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
    """Pull red01 + seal every local artifact into the test folder; never raises.

    This is the harness's half of the two-caller contract: destroy-competition.py runs the
    same collector for a run whose harness died, so the pull logic exists once
    (artifacts_ops.collect). It must run before `badauto destroy`, which erases red01; an
    unreachable box is recorded as `unreachable`, not raised, so a dead guest can never
    hold the destroy hostage."""
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        log("WARNING: no test folder on this run — artifact collection skipped")
        return None
    comp = core.REPO / "competitions" / args.competition
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
    """Best-effort `scrim-report.py <run_dir>` + fold its verdict into test.json.

    Generating INTERACTION.md used to be a manual operator step, which is why the machine
    verdict the artifact folder wants was usually missing. Both halves warn and continue:
    reporting must never block the destroy."""
    # main() sets run_dir; the test-folder fallback keeps the default case (run dir == test
    # folder) working for any caller that only knows args.test_dir.
    run_dir = Path(getattr(args, "run_dir", None) or getattr(args, "test_dir", None) or ".")
    try:
        r = procs.run(["python3", "scrim-report.py", str(run_dir)], cwd=core.REPO, timeout=300,
                      check=False)
        if r.returncode != 0:
            log(f"WARNING: scrim-report.py exited {r.returncode} — "
                f"{run_dir / 'INTERACTION.md'} may be stale")
    except Exception as e:
        log(f"WARNING: scrim-report.py failed: {e}")
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        return {}
    # ingest_verdict reads the test folder's OWN copy (evidence/harness/INTERACTION.md),
    # which the collection above cannot have taken: it ran before the report existed. Seal
    # the report where the collector would have put it so the verdict is readable (a later
    # teardown-time collection records it in collection.json with its hash).
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
