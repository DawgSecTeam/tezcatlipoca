"""Phase 6: the pre-domain pass (sshd/sudoers plants, then fix_services)."""

import json
import time

from common.constants import PER_MACHINE_NAKON_BUDGET
from guests.service_fixup_ops import fix_services_on_boxes
from nakonops.nakon_bundle_ops import build_nakon_bundle
from nakonops.nakon_run_ops import run_nakon
from common.timing import timed

from deploy_lib.coverage import record_clean_coverage, record_stage_coverage


def phase6_predomain_pass(ctx):
# [6/8] Pre-domain pass: sshd/sudoers plants, then fix_services.
#
#     Re-runnable by design: the .postclone-swept marker makes a resume skip the
#     whole sweep, and the nakon pass itself is strict=False (see the comment at the
#     call) because one flaky plant must not kill a sweep that 98% landed.
    if ctx.from_phase > 6:
        print("[6/8] Skipped (resume).")
        return
    swept_marker = ctx.comp_dir / ".postclone-swept"
    if swept_marker.exists():
        print("[6/8] Resume marker present — post-clone sweep already done; skipping")
    else:
        print("[6/8] Pre-domain pass (sshd/sudoers) on every team box...")
        predomain_machines = json.loads(ctx.predomain_config_path.read_text())["machines"]
        if predomain_machines:
            predomain_bundle = build_nakon_bundle(ctx.predomain_config_path)
            # strict=False: the sweep re-runs on every resume, and one flaky plant
            # must not kill the sweep after 98% of it landed. The golden plant
            # (phase 4) is the strict, authoritative one.
            with timed(ctx.comp_dir, 6, "nakon", f"predomain x{len(predomain_machines)}"):
                result = run_nakon(ctx.ssh_key, ctx.scoring_user, ctx.scoring_ip, predomain_bundle,
                                   ctx.predomain_config_path,
                                   timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(predomain_machines)),
                                   strict=False, jobs=ctx.nakon_jobs)
            # Stage-prefixed tally (verify's plant-integrity line names the pass)
            # and the structured coverage record (verify's plant-coverage gate).
            ctx.state["nakon_failed_steps"] = [f"predomain: {line}" for line in result.failed[:20]]
            record_stage_coverage(ctx.state, predomain_machines, result, ctx.save_state)
        else:
            print("  No pre-domain configurations in this lineup — pass skipped")
            # Still record the verdict. A lineup whose configs are ALL golden-stage
            # (same-type-2box-2026-09-29: apache/roundcube/bind + WinRM/IIS) never
            # reaches record_stage_coverage, so the key was never created and verify's
            # coverage gate failed closed on a completely clean run with "coverage was
            # never recorded (pre-tally deploy?)" (live-found 2026-10-02). "Nothing to
            # plant post-clone" is a clean result, not an absent one.
            record_clean_coverage(ctx)
        # fix_services right after the pre-domain pass: it un-wedges sshd (the ssh-*
        # configs above restart sshd and can trip the start-limit), creates the
        # credlist OS accounts, and binds the services the golden stage installed.
        # It must run BEFORE domains (nakon joins over SSH) and before the final
        # pass (whose disruptive configs would break its apt/SSH needs).
        with timed(ctx.comp_dir, 6, "fix_services"):
            fix_services_on_boxes(ctx.comp_dir, ctx.linux_targets, ctx.tf_ctx, box_creds=ctx.box_creds)
        swept_marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
