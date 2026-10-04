"""Phase 6: the post-clone repair sweep (sshd/sudoers plants, then fix_services)."""

import json
import time

from constants import PER_MACHINE_NAKON_BUDGET
from engine_ops import ensure_nat_forwarding
from hardening_ops import ensure_alpine_services, fix_services_on_boxes
from nakon_ops import build_nakon_bundle, run_nakon
from timing import timed
from utils import compfile_flag

from deploy_lib.coverage import record_clean_coverage, record_stage_coverage


def phase6_repair_sweep(ctx):
    """[6/8] Post-clone repair sweep: sshd/sudoers plants, then fix_services.

    Re-runnable by design: the .postclone-swept marker makes a resume skip the
    whole sweep, and the nakon pass itself is strict=False (see the comment at the
    call) because one flaky plant must not kill a sweep that 98% landed."""
    if ctx.from_phase > 6:
        print("[6/8] Skipped (resume).")
        return
    swept_marker = ctx.comp_dir / ".postclone-swept"
    if swept_marker.exists():
        print("[6/8] Resume marker present — post-clone sweep already done; skipping")
    else:
        print("[6/8] Repair-stage sweep (sshd/sudoers) on every team box...")
        ensure_nat_forwarding(ctx.tf_ctx)
        repair_machines = json.loads(ctx.repair_config_path.read_text())["machines"]
        if repair_machines:
            repair_bundle = build_nakon_bundle(ctx.repair_config_path)
            # strict=False: the sweep re-runs on every resume, and one flaky plant
            # must not kill the sweep after 98% of it landed. The golden plant
            # (phase 4) is the strict, authoritative one.
            with timed(ctx.comp_dir, 6, "nakon", f"repair x{len(repair_machines)}"):
                result = run_nakon(ctx.ssh_key, ctx.scoring_user, ctx.scoring_ip, repair_bundle,
                                   ctx.repair_config_path,
                                   timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(repair_machines)),
                                   strict=False, jobs=ctx.nakon_jobs)
            # Stage-prefixed tally (verify's plant-integrity line names the pass)
            # and the structured coverage record (verify's plant-coverage gate).
            ctx.state["nakon_failed_steps"] = [f"repair: {line}" for line in result.failed[:20]]
            record_stage_coverage(ctx.state, repair_machines, result, ctx.save_state)
        else:
            print("  No repair-stage configurations in this lineup — sweep skipped")
            # Still record the verdict. A lineup whose configs are ALL golden-stage
            # (same-type-2box-2026-09-29: apache/roundcube/bind + WinRM/IIS) never
            # reaches record_stage_coverage, so the key was never created and verify's
            # coverage gate failed closed on a completely clean run with "coverage was
            # never recorded (pre-tally deploy?)" (live-found 2026-10-02). "Nothing to
            # plant post-clone" is a clean result, not an absent one.
            record_clean_coverage(ctx)
        # fix_services right after the repair pass: it un-wedges sshd (the ssh-*
        # configs above restart sshd and can trip the start-limit), creates the
        # credlist OS accounts, and binds the services the golden stage installed.
        # It must run BEFORE domains (nakon joins over SSH) and before the final
        # pass (whose disruptive configs would break its apt/SSH needs).
        with timed(ctx.comp_dir, 6, "fix_services"):
            fix_services_on_boxes(ctx.comp_dir, ctx.linux_targets, ctx.tf_ctx, box_creds=ctx.box_creds)
            if compfile_flag(ctx.comp_dir / "Compfile", "alpine_services"):
                # Clones usually inherit the shim-installed services from the
                # golden disk; this pass is the idempotent safety net.
                ensure_alpine_services(ctx.comp_dir, ctx.linux_targets, ctx.tf_ctx)
        swept_marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
