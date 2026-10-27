"""Phase 6: the pre-domain pass (sshd/sudoers plants, then fix_services)."""

import time

from guests.converge_ops import record_clean_coverage, record_stage, run_stage, stage_machines
from guests.service_fixup_ops import fix_services_on_boxes
from common.timing import timed


def phase6_predomain_pass(ctx):
    if ctx.from_phase > 6:
        print("[6/8] Skipped (resume).")
        return
    swept_marker = ctx.comp_dir / ".postclone-swept"
    if ctx.from_phase == 6:
        # An explicit --from-phase 6 asks for the sweep again; the marker exists only to let a
        # LATER phase's resume skip work it knows landed.
        swept_marker.unlink(missing_ok=True)
    if swept_marker.exists():
        print("[6/8] Resume marker present — post-clone sweep already done; skipping")
        return
    print("[6/8] Pre-domain pass (sshd/sudoers) on every team box...")
    machines = stage_machines(ctx.predomain_config_path)
    if machines:
        # strict=False: the sweep re-runs on every resume, and one flaky plant
        # must not kill a sweep that 98% landed (phase 4's is the strict one)
        result = run_stage(ctx.predomain_config_path, machines, ssh_key=ctx.ssh_key,
                           scoring_user=ctx.scoring_user, scoring_ip=ctx.scoring_ip,
                           jobs=ctx.nakon_jobs, strict=False, label="predomain",
                           comp_dir=ctx.comp_dir)
        record_stage(ctx.state, machines, result, ctx.save_state, "predomain")
    else:
        print("  No pre-domain configurations in this lineup — pass skipped")
        # verify's coverage gate fails closed on a missing record: nothing to plant
        # post-clone is a clean result, not an absent one
        record_clean_coverage(ctx)
    # must run before domains (nakon joins over SSH) and before the final pass
    # (its disruptive configs break the apt/SSH needs this one depends on)
    with timed(ctx.comp_dir, 6, "fix_services"):
        fix_services_on_boxes(ctx.comp_dir, ctx.linux_targets, ctx.tf_ctx,
                              box_creds=ctx.box_creds)
    swept_marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
