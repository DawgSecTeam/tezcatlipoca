"""Phase 7: AD domains, the final disruption/boot-hostile pass, beacons, the tz-ready snapshot."""

import json
from functools import partial

from red.beacon_ops import plant_team_beacons
from common.constants import PER_MACHINE_NAKON_BUDGET, SNAP_READY
from guests.domain_ops import deploy_domain_configs
from guests.service_fixup_ops import reensure_mysql_credlist_users
from nakonops.nakon_bundle_ops import build_nakon_bundle
from nakonops.nakon_run_ops import run_nakon
from red.red_plant_ops import plant_assume_breach
from common.timing import timed
from common.utils import compfile_flag, run_concurrent

from deploy_lib.coverage import record_clean_coverage, record_stage_coverage
from deploy_lib.phases._snapshots import snap_ready


def _wipe_targets(managed_targets):
# clean_trails target rows from managed_targets.
#
#     The box record (name, template) is nested under t["box"] — reading top-level
#     keys silently yielded name="" windows=False for EVERY box, dispatching
#     Windows boxes into the Linux wipe path where they hung out their 180 s
#     budget (live 2026-10-11, dc01 NOT cleaned). fw01 needs no filter here:
#     unmanaged boxes never enter managed_targets.
    from common.windows_ops import is_windows_template
    return [{"ip": t["ip"],
             "name": (t.get("box") or {}).get("name") or "",
             "windows": is_windows_template((t.get("box") or {}).get("template", ""))}
            for t in managed_targets]


def _publish_bad_auto_state(ctx):
# bad-auto's nakon intel reads the box account from `<comp_dir>/.deploy_state.json`, its
#     terraform-era contract; the modern pipeline never wrote it, so every seed sprayed
#     `(box_user, None)`, paramiko failed with "no authentication methods", and cred_spray
#     reported the whole seed as `unreachable over SSH` on every box (live 2026-10-16).
#     The file rides to red01 with the other intel files. 0600: it holds the box password
#     and the credlist.
    path = ctx.comp_dir / ".deploy_state.json"
    path.write_text(json.dumps({
        "box_password": ctx.box_password,
        "box_creds": ctx.box_creds or {},
    }), encoding="utf-8")
    path.chmod(0o600)


def phase7_domains_and_final(ctx):
# [7/8] AD domains, then the final disruption/boot-hostile pass and beacons.
#
#     The final pass runs AFTER domain promotion on purpose: its disruptive configs
#     break the DNS/apt the Linux realmd joins need, and the boot-hostile ones would
#     brick a member box's domain-join reboot. From the final pass on, the boxes are
#     in their as-started competition flavor — nothing downstream reboots them or
#     needs apt/DNS.
    if ctx.from_phase > 7:
        print("[7/8] Skipped (resume).")
        return
    _publish_bad_auto_state(ctx)
    print("[7/8] Configuring Windows AD domains (if any), then the final "
          "disruption/boot-hostile pass and beacons...")
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
        final_bundle = build_nakon_bundle(ctx.final_config_path)
        with timed(ctx.comp_dir, 7, "nakon", f"final x{len(final_machines)}"):
            result = run_nakon(ctx.ssh_key, ctx.scoring_user, ctx.scoring_ip, final_bundle,
                               ctx.final_config_path,
                               timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(final_machines)),
                               strict=False, jobs=ctx.nakon_jobs)
        # Merge, not overwrite: a failure in BOTH passes must keep the pre-domain
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
    # Engine selection: `red.engine tlaloc` runs the v2 design (same red01, its
    # own loop/decisions/effects); anything else — including every existing
    # Compfile — keeps the v1 path. v2 is not the production red until it has
    # passed a scrim (migration plan phase 0/6).
    from red import tlaloc_ops
    tlaloc_selected = tlaloc_ops.red_engine(ctx.comp_dir) == "tlaloc"
    if tlaloc_selected or compfile_flag(ctx.comp_dir / "Compfile", "assume_breach"):
        if tlaloc_selected:
            print("  Planting assume-breach red presence (tlaloc v2: red01 + "
                  "C2 payloads + persistence)...")
            with timed(ctx.comp_dir, 7, "assume_breach"):
                red_out = tlaloc_ops.plant_assume_breach(ctx)
                # Red failing to deploy is a BLOCKER for a v2 comp (owner's directive,
                # 2026-10-16): warn-and-continue covers a range whose other components
                # are healthy, not one with no red team at all — the first live smoke
                # went "live" with red skipped entirely and looked successful.
                if not (red_out or {}).get("ok"):
                    raise RuntimeError(
                        "tlaloc v2 red deployment failed ("
                        f"{(red_out or {}).get('reason') or 'see degradations'}) — "
                        "refusing to leave a v2 competition without its red team")
        else:
            print("  Planting assume-breach red presence (red01 + beacons + persistence)...")
            with timed(ctx.comp_dir, 7, "assume_breach"):
                plant_assume_breach(ctx)

    # Account-file MODES last, after every account writer this deploy has: fix_services'
    # credlist useradd (phase 6) and the assume-breach seed's persistence user (above).
    # shadow-utils' useradd recreates /etc/passwd, /etc/shadow and /etc/group at their
    # default modes, so a mode planted in any earlier pass is already gone when verify
    # probes it. Before the trail wipe, so this pass's own SSH leaves no trace.
    late_machines = json.loads(ctx.late_config_path.read_text())["machines"]
    if late_machines:
        print(f"  Account-file mode pass on {len(late_machines)} machine(s) "
              f"(after the last account writer)...")
        late_bundle = build_nakon_bundle(ctx.late_config_path)
        with timed(ctx.comp_dir, 7, "nakon", f"late x{len(late_machines)}"):
            result = run_nakon(ctx.ssh_key, ctx.scoring_user, ctx.scoring_ip, late_bundle,
                               ctx.late_config_path,
                               timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(late_machines)),
                               strict=False, jobs=ctx.nakon_jobs)
        merged = list(ctx.state.get("nakon_failed_steps") or [])
        merged += [f"late: {line}" for line in result.failed[:20]]
        seen = set()
        ctx.state["nakon_failed_steps"] = [x for x in merged if not (x in seen or seen.add(x))][:40]
        record_stage_coverage(ctx.state, late_machines, result, ctx.save_state)

    # Trails first, snapshot second: the wipe has to be inside the restore point,
    # or a rollback to tz-ready hands blue every deploy log back.
    try:
        from scrim import clean_trails
        clean_trails.clean_trails(ctx.comp_dir, clean_trails.creds_for_deploy(ctx),
                                 targets=_wipe_targets(ctx.managed_targets))
    except Exception as e:                                   # noqa: BLE001 - never fatal
        print(f"  WARNING: trail wipe failed ({type(e).__name__}: {e}) — box logs still "
              f"hold the deploy")

    print(f"  Snapshotting all boxes as '{SNAP_READY}' (as-delivered restore point)...")
    run_concurrent(ctx.all_targets, partial(snap_ready, ctx), max_workers=4)
