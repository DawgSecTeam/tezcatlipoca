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
            # must not kill a sweep that 98% landed (phase 4's is the strict one)
            with timed(ctx.comp_dir, 6, "nakon", f"predomain x{len(predomain_machines)}"):
                result = run_nakon(ctx.ssh_key, ctx.scoring_user, ctx.scoring_ip, predomain_bundle,
                                   ctx.predomain_config_path,
                                   timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(predomain_machines)),
                                   strict=False, jobs=ctx.nakon_jobs)
            # verify's plant-integrity line and coverage gate read these, keyed by stage
            ctx.state["nakon_failed_steps"] = [f"predomain: {line}" for line in result.failed[:20]]
            record_stage_coverage(ctx.state, predomain_machines, result, ctx.save_state)
        else:
            print("  No pre-domain configurations in this lineup — pass skipped")
            # verify's coverage gate fails closed on a missing record: nothing to plant
            # post-clone is a clean result, not an absent one
            record_clean_coverage(ctx)
        # must run before domains (nakon joins over SSH) and before the final pass
        # (its disruptive configs break the apt/SSH needs this one depends on)
        with timed(ctx.comp_dir, 6, "fix_services"):
            fix_services_on_boxes(ctx.comp_dir, ctx.linux_targets, ctx.tf_ctx, box_creds=ctx.box_creds)
        swept_marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
