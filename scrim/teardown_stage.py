import json
import os

from common.comp_paths import resolve_comp_dir
from common.utils import competition_name, load_compfile

from scrim import core
from scrim import red_link
from scrim import test_folder
from scrim.core import log


def _manifest_red_vmid(args):
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        return None
    try:
        from artifacts_lib import artifacts_ops
        red = ((artifacts_ops.load_manifest(test_dir).get("agents") or {})
               .get("red") or {})
        return red.get("vmid") or None
    except Exception:                                        # noqa: BLE001 - best effort
        return None


def _red_vm_still_exists(vmid):
    """None (could not ask) counts as not-proven-gone, never as success."""
    if not vmid:
        return None
    try:
        from infra.client import live_vmids
        return int(vmid) in live_vmids()
    except Exception as e:                                  # noqa: BLE001 - reported
        log(f"WARNING: could not verify red01 vmid {vmid} is gone: {e}")
        return None


def teardown_red(args, env):
    """Destroy this run's red01 and prove the recorded vmid is gone; returns it for verify_clean journaling."""
    log("teardown: red01 + the red segment rules")
    try:
        red_link.pull_red_state(args)
    except Exception as e:                                   # noqa: BLE001 - never blocks
        log(f"WARNING: red state pull failed before teardown: {type(e).__name__}: {e}")
    comp_path = str(resolve_comp_dir(args.competition,
                                     getattr(args, "comp_dir", None)).resolve())
    from estate import config as estate_config
    from infra.ownership import Ownership
    from red import red01_ops, tlaloc_ops
    identity = red01_ops.recorded_identity(comp_path)
    flat = {}
    try:
        flat = core.read_state(comp_path) or {}
    except Exception:                                        # noqa: BLE001 - optional
        pass
    vmid = (args.red_vmid or identity.get("vmid") or _manifest_red_vmid(args)
            or (flat.get("red") or {}).get("vmid"))
    if not vmid:
        log("teardown: red01 — this run recorded no red01; nothing to destroy")
        return None
    tlaloc_ops.destroy_tlaloc_red(
        comp_path,
        ownership=Ownership(competition_name(comp_path), flat.get("run_id") or ""),
        engine_ip=flat.get("engine_mgmt_ip") or "",
        engine_user=estate_config.active().engine.username,
        ssh_key=core.ssh_key_path(), env=env, red_ip=identity.get("ip"))
    still = _red_vm_still_exists(vmid)
    if still:
        raise RuntimeError(f"red01 vmid {vmid} is STILL PRESENT after destroy — the "
                           f"range is not torn down")
    if still is None:
        log(f"teardown: red01 vmid {vmid} could not be verified gone — check by hand")
    else:
        log(f"teardown: red01 vmid {vmid} confirmed gone")
    return vmid


def run_range_teardown(args, *, evidence_done=False, red_vmid=None):
    """Same terminal teardown the standalone CLI runs, in-process; journalled harness steps skip re-running against dead boxes."""
    from teardown import Refusal
    from teardown.runner import record_evidence_done, run_terminal_teardown

    comp_dir = resolve_comp_dir(args.competition, getattr(args, "comp_dir", None)).resolve()
    teams = json.loads((comp_dir / "teams.json").read_text())
    boxes = json.loads((comp_dir / "boxes.json").read_text())
    name, _, _ = load_compfile(comp_dir / "Compfile")
    # the red01 absence proof is independent of the artifact outcome, so it is journalled either way
    record_evidence_done(comp_dir,
                         test_dir=getattr(args, "test_dir", None) if evidence_done else None,
                         red_vmid=red_vmid)
    try:
        report = run_terminal_teardown(
            comp_dir, teams, boxes, name, env={**os.environ}, log=log,
            # --keep-templates: this comp's next cycle reuses the tested golden + engine templates
            full=not getattr(args, "keep_templates", False))
    except Refusal as r:
        raise RuntimeError(f"range teardown refused: {r}") from r
    if not report.clean:
        raise RuntimeError("range teardown did not come back clean:\n"
                           + report.render())


def stage_teardown(args, creds=None):
    tunnel = getattr(args, "red_tunnel", None)
    if hasattr(tunnel, "shutdown"):
        tunnel.shutdown()
        log("teardown: red LLM tunnel stopped")
    collected = None
    if creds:
        # The final red pull MUST land BEFORE the report: it grades red's own world.json, and
        # pulling only inside teardown_red made the report grade red's PREVIOUS state.
        try:
            red_link.pull_red_state(args)
        except Exception as e:  # evidence capture must never block teardown
            log(f"WARNING: red state pull failed before the report: {type(e).__name__}: {e}")
        # Must run before the red01 destroy below, which erases it. Never raises: an
        # unreachable red01 is recorded as `unreachable` in collection.json.
        collected = test_folder.collect_run_artifacts(args)
        test_folder.write_interaction_report(args)
    log("teardown: red01 + NAT")
    red_vmid = teardown_red(args, dict(os.environ))
    if args.keep_range:
        log("--keep-range: leaving the competition range up")
        return
    if getattr(args, "keep_templates", False):
        log("--keep-templates: templates kept for this competition's next run "
            "(team boxes, engine and bridges still torn down)")
    log("teardown: competition range")
    run_range_teardown(args, evidence_done=bool(collected), red_vmid=red_vmid)
