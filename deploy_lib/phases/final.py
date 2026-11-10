"""Phase 7: AD domains, the final disruption/boot-hostile pass, red, the tz-ready snapshot."""

import json
from functools import partial

from common.constants import SNAP_READY
from guests.converge_ops import record_clean_coverage, record_stage, run_stage, stage_machines
from guests.domain_ops import deploy_domain_configs
from guests.service_fixup_ops import reensure_mysql_credlist_users
from common.timing import timed
from common.utils import compfile_flag, run_concurrent
from red import tlaloc_ops

from deploy_lib.phases._snapshots import snap_ready


def _publish_red_intel_state(ctx):
    """tlaloc's sensors read the scoreboard as admin; the file holds box passwords.

    It is a staged INPUT to tlaloc's intel contract (tlaloc/intel/base.py), not a second copy
    of the state document: the agent only ever sees what the deploy stages onto red01.
    """
    from config.config_ops import write_text_atomic
    write_text_atomic(ctx.comp_dir / ".deploy_state.json", json.dumps({
        "box_password": ctx.box_password,
        "box_creds": ctx.box_creds or {},
        "admin_password": ctx.admin_password,
    }))



def _ensure_pinned_ftp(ctx):
    """The IIS FTP helper starts FTPSVC before setting it Automatic; if the feature
    install leaves it running, the start type stays Disabled and the next boot loses
    the scored port."""
    from infra.guest_exec import guest_agent_exec_windows
    path = ctx.comp_dir / "box_services.json"
    if not path.exists():
        return
    services = json.loads(path.read_text())
    for target in ctx.managed_targets:
        box = target.get("box") or {}
        pins = services.get(box.get("name")) or []
        names = [(pin.get("name") if isinstance(pin, dict) else pin) for pin in pins]
        if "IIS FTP" not in names:
            continue
        rc, _out, err = guest_agent_exec_windows(
            target.get("node") or ctx.node, target["vmid"],
            "Set-Service FTPSVC -StartupType Automatic; Start-Service FTPSVC",
            timeout=180)
        if rc != 0:
            raise RuntimeError(f"could not enable FTPSVC on {box.get('name')}: {err[-300:]}")

def phase7_domains_and_final(ctx):
    if ctx.from_phase > 7:
        print("[7/8] Skipped (resume).")
        return
    _publish_red_intel_state(ctx)
    print("[7/8] Configuring Windows AD domains (if any), then the final "
          "disruption/boot-hostile pass and beacons...")
    with timed(ctx.comp_dir, 7, "domains"):
        deploy_domain_configs(ctx.teams, ctx.boxes, ctx.comp_dir, ctx.nakon_config_path,
                              ctx.ssh_key, ctx.scoring_user, ctx.scoring_ip, ctx.box_password)

    # AFTER domain promotion on purpose: these configs break the DNS/apt realmd joins need and
    # would brick a member box's domain-join reboot; nothing downstream reboots or needs apt.
    final_machines = stage_machines(ctx.final_config_path)
    if not final_machines:
        record_clean_coverage(ctx)
    else:
        print(f"  Final-stage pass (disruption + boot-hostile) on "
              f"{len(final_machines)} machine(s)...")
        result = run_stage(ctx.final_config_path, final_machines, ssh_key=ctx.ssh_key,
                           scoring_user=ctx.scoring_user, scoring_ip=ctx.scoring_ip,
                           jobs=ctx.nakon_jobs, strict=False, label="final",
                           comp_dir=ctx.comp_dir)
        record_stage(ctx.state, final_machines, result, ctx.save_state, "final")

    # The mysql final-stage plants rebuild the auth tables and take the credlist SQL accounts
    # fix_services created with them, failing the auth-based sql check on every team copy.
    reensure_mysql_credlist_users(ctx.comp_dir, ctx.linux_targets, ctx.tf_ctx, ctx.box_creds)

    if compfile_flag(ctx.comp_dir / "Compfile", "team_beacons"):
        print("  ERROR: `team_beacons` belonged to the earlier agent and no longer exists — remove it "
              "from the Compfile")
        raise RuntimeError("team_beacons is not supported")

    _ensure_pinned_ftp(ctx)

    # Red goes in before the snapshot: the restore point every box carries must already be
    # compromised, and a range with no red team aborts the deploy rather than degrade.
    print("  Planting assume-breach red presence (tlaloc: red01 + C2 + persistence)...")
    with timed(ctx.comp_dir, 7, "red"):
        # The loop starts at the END of this phase: a live agent would generate traffic while
        # the late pass still writes account files, the trail wipe still runs and tz-ready is
        # still being taken.
        red_summary = tlaloc_ops.plant_assume_breach(ctx, start_loop=False)

    # Last of the account writers (useradd resets /etc/{passwd,shadow,group} to default modes,
    # undoing any earlier plant), but before the trail wipe so this pass leaves no SSH trace.
    late_machines = stage_machines(ctx.late_config_path)
    if late_machines:
        print(f"  Account-file mode pass on {len(late_machines)} machine(s) "
              f"(after the last account writer)...")
        result = run_stage(ctx.late_config_path, late_machines, ssh_key=ctx.ssh_key,
                           scoring_user=ctx.scoring_user, scoring_ip=ctx.scoring_ip,
                           jobs=ctx.nakon_jobs, strict=False, label="late",
                           comp_dir=ctx.comp_dir)
        record_stage(ctx.state, late_machines, result, ctx.save_state, "late")

    # Trails first, snapshot second: the wipe has to be inside the restore point,
    # or a rollback to tz-ready hands blue every deploy log back.
    try:
        from guests import clean_trails
        clean_trails.clean_trails(ctx.comp_dir, clean_trails.creds_for_deploy(ctx),
                                 targets=clean_trails.targets_from(ctx.managed_targets))
    except Exception as e:                                   # noqa: BLE001 - never fatal
        print(f"  WARNING: trail wipe failed ({type(e).__name__}: {e}) — box logs still "
              f"hold the deploy")

    print(f"  Snapshotting all boxes as '{SNAP_READY}' (as-delivered restore point)...")
    run_concurrent(ctx.all_targets, partial(snap_ready, ctx), max_workers=4)

    # Last: the range is delivered, so red may begin attacking — UNLESS the start is held.
    begin_red_after_delivery(ctx, red_summary)


def begin_red_after_delivery(ctx, red_summary):
    # A --hold-start range is not delivered yet: run-14ac7f14 released the loop at deploy
    # end and tlaloc stripped freshly-provisioned forwarders 4.5 hours before T0. The unit
    # stays enabled-but-stopped; competition-ops.py start (or scrim's T0) begins play.
    if not red_summary:
        return
    if ctx.state.get("hold_start"):
        ctx.state["red_loop_held"] = True
        ctx.save_state()
        print("  tlaloc      : loop HELD (--hold-start) — red starts with the event "
              "(competition-ops.py start releases it)")
        return
    with timed(ctx.comp_dir, 7, "red_start"):
        tlaloc_ops.start_director(ctx.ssh_key, red_summary["red_ip"])
    print(f"  tlaloc      : loop started on red01 ({red_summary['red_ip']})")
