"""Phase 7: AD domains, the final disruption/boot-hostile pass, beacons, the tz-ready snapshot."""

import json
from functools import partial

from beacon_ops import plant_team_beacons
from constants import PER_MACHINE_NAKON_BUDGET, SNAP_READY
from domain_ops import deploy_domain_configs
from engine_ops import ensure_nat_forwarding
from hardening_ops import reensure_mysql_credlist_users
from nakon_ops import build_nakon_bundle, run_nakon
from red_plant_ops import plant_assume_breach
from timing import timed
from utils import compfile_flag, run_concurrent

from deploy_lib.coverage import record_clean_coverage, record_stage_coverage
from deploy_lib.phases._snapshots import snap_ready


def phase7_domains_and_final(ctx):
    """[7/8] AD domains, then the final disruption/boot-hostile pass and beacons.

    The final pass runs AFTER domain promotion on purpose: its disruptive configs
    break the DNS/apt the Linux realmd joins need, and the boot-hostile ones would
    brick a member box's domain-join reboot. From the final pass on, the boxes are
    in their as-started competition flavor — nothing downstream reboots them or
    needs apt/DNS."""
    if ctx.from_phase > 7:
        print("[7/8] Skipped (resume).")
        return
    print("  Configuring Windows AD domains (if any)...")
    with timed(ctx.comp_dir, 7, "domains"):
        deploy_domain_configs(ctx.teams, ctx.boxes, ctx.comp_dir, ctx.nakon_config_path,
                              ctx.ssh_key, ctx.scoring_user, ctx.scoring_ip, ctx.box_password)

    # Final-stage pass AFTER domains: the disruptive configs break the DNS/apt the
    # Linux realmd joins need, and the boot-hostile configs would brick any member
    # box's domain-join reboot. From here on the boxes are in their as-started
    # competition flavor — nothing downstream reboots them or needs apt/DNS.
    final_machines = json.loads(ctx.final_config_path.read_text())["machines"]
    if not final_machines:
        record_clean_coverage(ctx)
    if final_machines:
        print(f"  Final-stage pass (disruption + boot-hostile) on {len(final_machines)} machine(s)...")
        ensure_nat_forwarding(ctx.tf_ctx)
        final_bundle = build_nakon_bundle(ctx.final_config_path)
        with timed(ctx.comp_dir, 7, "nakon", f"final x{len(final_machines)}"):
            result = run_nakon(ctx.ssh_key, ctx.scoring_user, ctx.scoring_ip, final_bundle,
                               ctx.final_config_path,
                               timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(final_machines)),
                               strict=False, jobs=ctx.nakon_jobs)
        # Merge, not overwrite: a failure in BOTH passes must keep the repair
        # tally (phase 6 used to erase it by writing only on failure).
        merged = list(ctx.state.get("nakon_failed_steps") or [])
        merged += [f"final: {line}" for line in result.failed[:20]]
        seen = set()
        ctx.state["nakon_failed_steps"] = [x for x in merged if not (x in seen or seen.add(x))][:40]
        # Save unconditionally: record_coverage may have CLEARED a stale
        # coverage entry on this fully-green pass, and a clear that never
        # reaches disk leaves verify's coverage gate red (see
        # record_stage_coverage — the ff9b19f bug, re-created).
        record_stage_coverage(ctx.state, final_machines, result, ctx.save_state)

    # The mysql final-stage plants rebuild the auth tables, taking the credlist SQL
    # accounts fix_services created pre-plant with them — the auth-based sql check
    # then fails on every team copy (2026-09-30 testcomp-7box: db01-sql DOWN at first
    # verify, users re-added by hand mid-prep). Idempotent; mysql-pinned boxes only.
    reensure_mysql_credlist_users(ctx.comp_dir, ctx.linux_targets, ctx.tf_ctx, ctx.box_creds)

    if compfile_flag(ctx.comp_dir / "Compfile", "team_beacons"):
        print("  Planting team beacons (hunt artifacts)...")
        with timed(ctx.comp_dir, 7, "beacons"):
            plant_team_beacons(ctx.teams, ctx.boxes, ctx.tf_ctx, box_username=ctx.box_username,
                               box_password=ctx.box_password)

    # Assume-breach (CCDC realism): red is ALREADY inside when the clock starts.
    # Deploy red01 + the realm engine DNAT and run the day-0 seed (access +
    # prebaked Realm C2 beacons + the persistence/evasion layer) BEFORE the
    # snapshot, so the restore point every box carries is already-compromised
    # and phase 8 starts the clock on a range red owns.
    if compfile_flag(ctx.comp_dir / "Compfile", "assume_breach"):
        print("  Planting assume-breach red presence (red01 + beacons + persistence)...")
        with timed(ctx.comp_dir, 7, "assume_breach"):
            plant_assume_breach(ctx)

    # Trails first, snapshot second: the wipe has to be inside the restore point,
    # or a rollback to tz-ready hands blue every deploy log back.
    try:
        from scrim import clean_trails
        clean_trails.clean_trails(ctx.comp_dir, clean_trails.creds_for_deploy(ctx),
                                 targets=[{"ip": t["ip"], "name": t.get("box_name") or "",
                                           "windows": bool(t.get("windows"))}
                                          for t in ctx.managed_targets
                                          if (t.get("box_name") or "") != "fw01"])
    except Exception as e:                                   # noqa: BLE001 - never fatal
        print(f"  WARNING: trail wipe failed ({type(e).__name__}: {e}) — box logs still "
              f"hold the deploy")

    print(f"  Snapshotting all boxes as '{SNAP_READY}' (as-delivered restore point)...")
    run_concurrent(ctx.all_targets, partial(snap_ready, ctx), max_workers=4)
