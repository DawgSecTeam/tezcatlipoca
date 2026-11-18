"""Phase 7: AD domains, the final disruption/boot-hostile pass, red staging, trail wipe."""

import json
from guests.converge_ops import record_clean_coverage, record_stage, run_stage, stage_machines
from guests.domain_ops import deploy_domain_configs
from guests.service_fixup_ops import reensure_mysql_credlist_users
from common.timing import timed
from common.utils import compfile_flag, run_concurrent
from red import c3rberus_ops, tlaloc_ops


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

    # Red is STAGED before the snapshot, not deployed: the restore point every box carries
    # must be a built range nobody has compromised yet. A range with no red team still aborts
    # the deploy rather than degrade. The loop starts at the END of this phase: a live agent
    # would generate traffic while the late pass writes account files, the trail wipe runs and
    # tz-ready is taken.
    print("  Preparing assume-breach red presence (tlaloc: red01 + C2 + payload matrix)...")
    with timed(ctx.comp_dir, 7, "red_prepare"):
        red_summary = tlaloc_ops.plant_assume_breach(
            ctx, start_loop=False, deploy_implants_now=False)
    # The c3rberus platform installs beside tlaloc on the same red01, and its day-0
    # IOC catalog must be inside tz-ready too — blue restores ONTO the IOCs, and the
    # platform scores exactly that remediation.
    if c3rberus_ops.selected(ctx.comp_dir):
        with timed(ctx.comp_dir, 7, "red_c3rberus"):
            red_summary["c3rberus"] = c3rberus_ops.stage(
                ctx.comp_dir, ssh_key=ctx.ssh_key, red_ip=red_summary["red_ip"],
                box_pw=ctx.box_password)

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

    # The wipe belongs inside the prepared restore point: without it a reset hands blue
    # every deploy log back.
    _wipe_trails(ctx)


def _wipe_trails(ctx):
    try:
        from guests import clean_trails
        clean_trails.clean_trails(ctx.comp_dir, clean_trails.creds_for_deploy(ctx),
                                 targets=clean_trails.targets_from(ctx.managed_targets))
    except Exception as e:                                   # noqa: BLE001 - never fatal
        print(f"  WARNING: trail wipe failed ({type(e).__name__}: {e}) — box logs still "
              f"hold the deploy")


def begin_red_after_delivery(ctx, red_summary):
    # A --hold-start range is not delivered yet: run-14ac7f14 released the loop at deploy
    # end and tlaloc stripped freshly-provisioned forwarders 4.5 hours before T0. The unit
    # stays enabled-but-stopped; competition-ops.py start (or scrim's T0) begins play.
    #
    # A RESUME inherits the hold the range was deployed with: the flag rides the command
    # line, but a resume re-enters from the state document and its caller has no reason to
    # repeat it — run-2cd2e47c's resume therefore started the loop 12 minutes before T0 and
    # tlaloc masked mysql on a box that was never scored for it, leaving 4 pins down when
    # the scrim's pre-T0 gate read the estate. The document is the truth.
    if not red_summary:
        return
    if ctx.state.get("hold_start") or ctx.state.get("red_loop_held"):
        ctx.state["hold_start"] = True
        ctx.state["red_loop_held"] = True
        ctx.save_state()
        print("  tlaloc      : range PREPARED, not armed (--hold-start) — the implants deploy "
              "and red starts with the event (competition-ops.py start releases it)")
        return
    with timed(ctx.comp_dir, 8, "red_deploy"):
        tlaloc_ops.deploy_implants(ctx.comp_dir, ctx.state, ssh_key=ctx.ssh_key)
        ctx.state["red_deployed"] = True
        ctx.state.pop("red_staged", None)
        ctx.save_state()
    with timed(ctx.comp_dir, 8, "red_start"):
        tlaloc_ops.start_director(ctx.ssh_key, red_summary["red_ip"])
    print(f"  tlaloc      : loop started on red01 ({red_summary['red_ip']})")
    if c3rberus_ops.selected(ctx.comp_dir):
        # the platform's own T0: box-touching loops on, comp started, scheduler fires
        c3rberus_ops.begin(ctx.comp_dir, ssh_key=ctx.ssh_key,
                           red_ip=red_summary["red_ip"])
