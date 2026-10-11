import json
import os
from pathlib import Path

from config_ops import write_text_atomic

from scrim import core
from scrim import endpoints
from scrim import procs
from scrim import red_link
from scrim import test_folder
from scrim.core import log


def _config_red_vmid():
    """The red vmid bad-auto's destroy will actually act on: config.yaml's deploy.red_vmid.

    `badauto destroy` follows config.yaml by design (cmd_destroy refuses to be a silent
    target override), so this is the number to reconcile `--red-vmid` against."""
    try:
        cfg = json.loads((core.BAD_AUTO / "config.yaml").read_text())
        return (cfg.get("deploy") or {}).get("red_vmid")
    except (OSError, ValueError):
        return None


def _manifest_red_vmid(args):
    """red's vmid from this run's manifest, when the run recorded one."""
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        return None
    try:
        import artifacts_ops
        red = ((artifacts_ops.load_manifest(test_dir).get("agents") or {})
               .get("red") or {})
        return red.get("vmid") or None
    except Exception:                                        # noqa: BLE001 - best effort
        return None


def _red_vm_still_exists(vmid):
    """True when `vmid` is still present in the cluster.

    None (could not ask) is treated as "not proven gone" by the caller — this is a
    teardown assertion, so an unverifiable check must not read as success."""
    if not vmid:
        return None
    try:
        from range_ops import live_vmids
        return int(vmid) in live_vmids()
    except Exception as e:                                  # noqa: BLE001 - reported
        log(f"WARNING: could not verify red01 vmid {vmid} is gone: {e}")
        return None


def teardown_red(args, env):
    """Destroy red01 + its NAT, then PROVE the VM is gone.

    scale8 soak 2026-10-02: red01 (998) survived `badauto destroy` and had to be
    destroyed by hand. The stage ran with check=False and never looked at the result, so
    a destroy that removed nothing was indistinguishable from one that worked — the
    driver printed DONE while a red box with a live LLM key and beacon tasking stayed
    up on the range. Both halves are needed: surface the exit code, and assert the
    specific vmid this run deployed."""
    log("teardown: red01 + NAT")
    # Pull what red actually planted BEFORE destroy erases the VM. The artifact
    # collector also lists world.json, but it treats red01 as one target and skips
    # every remaining file once the first scp fails — so the record the persistence
    # verification depends on is fetched out-of-band here, via sudo-staged copies.
    try:
        red_link.pull_red_state(args)
    except Exception as e:  # evidence capture must never block teardown
        log(f"WARNING: red state pull failed before teardown: {type(e).__name__}: {e}")
    configured = _config_red_vmid()
    if args.red_vmid and configured and int(configured) != int(args.red_vmid):
        # Not fatal — badauto's identity guards are the authority on what is safe to
        # delete — but it is exactly the stale-config shape that lost red01, so say it.
        log(f"WARNING: config.yaml's deploy.red_vmid is {configured} but this run deployed "
            f"{args.red_vmid}; badauto destroy follows config.yaml")
    # One retry: the first attempt's rc=1 can be a transient Proxmox task/API hiccup,
    # and the fail-loud raise below aborts the whole teardown mid-sequence (the range
    # destroy never runs, and the harness dies leaving everything up — live-found
    # 2026-10-03: the manual re-run succeeded in seconds). Identity guards live inside
    # badauto, so a retry cannot widen what may be deleted.
    proc = None
    # The --competition value must be the SAME string stage_red wrote into config.yaml's
    # competition_dir (the resolved path): bad-auto's destroy cross-checks the two and
    # refuses a mismatch (both 2026-10-03 teardowns failed on the bare name).
    comp_path = str((core.REPO / "competitions" / args.competition).resolve())
    for attempt in (1, 2):
        proc = procs.run(["python3", "-m", "badauto", "destroy", "--competition",
                          comp_path, "--yes"],
                         cwd=core.BAD_AUTO, env=env, timeout=900, check=False)
        if proc.returncode == 0:
            break
        if attempt == 1:
            log(f"WARNING: badauto destroy rc={proc.returncode} on attempt 1 — "
                f"retrying once before failing the teardown"
                + (f"; stderr: {(proc.stderr or '')[-300:]}" if proc.stderr else ""))
        else:
            # Leave the evidence where the post-mortem will look: the test folder
            # survives the harness crash and the archive carries it out of the worktree.
            test_dir = getattr(args, "test_dir", None)
            if test_dir:
                try:
                    write_text_atomic(
                        Path(test_dir) / "bad-auto-destroy.log",
                        (proc.stdout or "") + "\n=== STDERR ===\n" + (proc.stderr or ""),
                        mode=0o600)
                except OSError as e:
                    log(f"WARNING: could not write bad-auto-destroy.log: {e}")
            raise RuntimeError(
                f"badauto destroy failed (rc={proc.returncode}) — red01 and its engine NAT "
                f"rules may still be up. stderr tail: {(proc.stderr or '(captured nothing)')[-500:]}. "
                f"Do not treat this run as torn down; fix and re-run "
                f"`python3 -m badauto destroy --competition {args.competition} --yes` in "
                f"{core.BAD_AUTO}, or destroy vmid {args.red_vmid} by hand and re-run "
                f"destroy-competition.py.")

    # Order matters: what this run deployed, then what the run recorded, then what
    # config.yaml says, then bad-auto's own default — one of them is the number
    # `badauto destroy` acted on, and an unknown vmid is an unverifiable teardown
    # (2026-10-08 run 6b logged "vmid None could not be verified gone" because
    # --red-vmid was not passed and the config omits it).
    vmid = (args.red_vmid or _manifest_red_vmid(args) or configured
            or core.bad_auto_deploy_default("red_vmid"))
    still = _red_vm_still_exists(vmid)
    if still:
        raise RuntimeError(
            f"badauto destroy reported success but vmid {vmid} is STILL PRESENT — red01 "
            f"survived teardown. This is the scale8 soak's leak (red01 998 left running, "
            f"with the beacon controller and its LLM key, after the driver printed DONE). "
            f"Destroy vmid {vmid} before starting another run on this range.")
    if still is False:
        log(f"teardown: red01 vmid {vmid} confirmed gone")
    else:
        log(f"teardown: red01 vmid {vmid} could not be verified gone — check by hand "
            f"(badauto destroy exited 0)")


def stage_teardown(args, creds=None):
    tunnel = getattr(args, "red_tunnel", None)
    if hasattr(tunnel, "shutdown"):
        tunnel.shutdown()
        log("teardown: red LLM tunnel stopped")
    if creds:
        # The final red pull MUST land BEFORE the report is generated. The report grades
        # red's own world.json (footholds, artifact set, beacon score), but that pull used
        # to live only inside teardown_red — i.e. AFTER write_interaction_report — so the
        # report raced it and graded red's PREVIOUS state. Live run4 2026-10-09: world.json
        # held 2 Windows footholds while the report written 2 s earlier read 0, and the
        # rehearsal's windows_footholds gate failed against a world that did not exist yet.
        try:
            red_link.pull_red_state(args)
        except Exception as e:  # evidence capture must never block teardown
            log(f"WARNING: red state pull failed before the report: {type(e).__name__}: {e}")
        # The collector owns the red01 pull now — one implementation shared with
        # destroy-competition.py (artifacts_ops.py's docstring: two callers must agree) —
        # and it MUST run before `badauto destroy` below, which erases red01. It never
        # raises: an unreachable red01 is recorded as `unreachable` in collection.json.
        test_folder.collect_run_artifacts(args)
        test_folder.write_interaction_report(args)
    log("teardown: red01 + NAT")
    env = {**os.environ, "BAuto_LLM_API_KEY": endpoints.api_key(local=True)}
    teardown_red(args, env)
    if args.keep_range:
        log("--keep-range: leaving the competition range up")
        return
    log("teardown: competition range")
    procs.run(["python3", "destroy-competition.py", "--competition", args.competition, "--yes"],
              cwd=core.REPO, timeout=3600)
