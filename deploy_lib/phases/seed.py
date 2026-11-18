"""Phase 8: seed teams, unpause the engine, create injects (each once), then the opt-in portal."""

from config.injects_config import injects_fingerprint, resolve_inject_times
from engine.engine_event_ops import ensure_event_conf_current
from portal.portal_ops import deploy_portal
from quotient.setup import create_injects, engine_paused, seed_teams, unpause_engine
from common.ssh_ops import wait_for_http
from common.timing import timed
from deploy_lib.phases._snapshots import snap_prepared


def phase8_seed(ctx):
    if ctx.from_phase > 8:
        print("[8/8] Skipped (resume).")
        return
    held = bool(ctx.state.get("hold_start"))
    print("[8/8] Seeding competition and creating injects...")

    with timed(ctx.comp_dir, 8, "wait_quotient_http"):
        wait_for_http(f"http://{ctx.scoring_ip}/api/login", timeout=120)

    # The final disruption pass (phase 7) runs AFTER phase 6's fix_services and can leave a
    # scored service stopped or its credlist accounts rebuilt away — the estate then starts
    # with pins already DOWN, which scores nothing and blames red (live: scrim-one, 6/12 and
    # 5/12 at T0 and at the bell). Re-ensure now, with the engine up and before the clock.
    from guests.service_fixup_ops import fix_services_on_boxes
    if (ctx.comp_dir / "box_services.json").exists() and ctx.linux_targets:
        with timed(ctx.comp_dir, 8, "reensure_services"):
            fix_services_on_boxes(ctx.comp_dir, ctx.linux_targets, ctx.tf_ctx,
                                  box_creds=ctx.box_creds)
    # A resume re-runs this phase after the comp files were fixed, but only phase 3 ever
    # pushed event.conf — without this, the engine keeps scoring the pre-fix pin set
    # (run-14ac7f14 → run-bd70a985 phantom win01-iis). No-op when nothing changed.
    if ctx.boxes and ctx.box_creds:
        with timed(ctx.comp_dir, 8, "event_conf_drift"):
            fresh = ensure_event_conf_current(
                ctx.comp_dir, ctx.teams, ctx.boxes, ctx.tf_ctx, ctx.name,
                admin_password=ctx.admin_password, scoring_password=ctx.scoring_password,
                inject_password=ctx.inject_password, box_creds=ctx.box_creds,
                extra_credlists=({"domain": ctx.domain_creds} if ctx.domain_creds else None))
        if fresh is False:
            ctx.state["event_conf_repushed"] = True
            ctx.save_state()
            with timed(ctx.comp_dir, 8, "wait_quotient_after_repush"):
                wait_for_http(f"http://{ctx.scoring_ip}/api/login", timeout=180)

    quotient_ctx = {
        "teams": {team_key: team_data["identifier"] for team_key, team_data in ctx.teams.items()},
        "quotient_admin_password": ctx.admin_password,
    }

    if not ctx.state.get("seeded"):
        print("  Seeding teams" + (" (start HELD — clock not started)" if held
                                   else " and starting the competition clock..."))
        with timed(ctx.comp_dir, 8, "seed_teams"):
            seed_teams(ctx.scoring_ip, quotient_ctx, start=not held)
        ctx.state["seeded"] = True
        ctx.save_state()
    else:
        print("  Teams already seeded (resume) — skipping.")

    # One prepared point for the WHOLE range, after every box-touching prep step: a round
    # resets all three VM classes here, so it cannot pair a pre-seed red01 with armed boxes
    # or an engine expecting work the boxes no longer hold. Skipped once the range is armed.
    if not ctx.state.get("red_deployed"):
        snap_prepared(ctx)

    if held:
        # the hold outlives the deploy: engine stays paused and the clock unstarted
        # until competition-ops.py start flips both, so those flags stay unset
        print("  Start is HELD (--hold-start): the engine stays paused and injects are "
              "deferred — begin the competition with:")
        print(f"      python3 competition-ops.py start --comp-dir {ctx.comp_dir}")
    else:
        if not ctx.state.get("engine_unpaused"):
            # the unpause POST is not idempotent: a resume asks the engine first
            paused = engine_paused(ctx.scoring_ip, quotient_ctx)
            if paused is False:
                print("  Engine reports itself unpaused — recording and skipping.")
            else:
                with timed(ctx.comp_dir, 8, "unpause_engine"):
                    unpause_engine(ctx.scoring_ip, quotient_ctx)
            ctx.state["engine_unpaused"] = True
            ctx.save_state()
        else:
            print("  Engine already unpaused (resume) — skipping.")

        # `injects_created` cannot tell current from stale: the fingerprint gates
        # re-entry and must be taken BEFORE resolve_inject_times pops the offsets
        want_injects = injects_fingerprint(ctx.injects) if ctx.injects else None
        if ctx.injects and (not ctx.state.get("injects_created")
                            or ctx.state.get("injects_fingerprint") != want_injects):
            print(f"  Creating {len(ctx.injects)} inject(s)...")
            resolve_inject_times(ctx.injects)
            with timed(ctx.comp_dir, 8, "create_injects", f"x{len(ctx.injects)}"):
                _created, failed_titles = create_injects(ctx.scoring_ip, ctx.admin_password, ctx.injects)
            if failed_titles:
                print(f"  WARNING: {len(failed_titles)} inject(s) failed to create: "
                      f"{', '.join(failed_titles)} — re-run --from-phase 7 to retry "
                      f"(existing injects are deduped)")
            else:
                ctx.state["injects_created"] = True
                ctx.state["injects_fingerprint"] = want_injects
                ctx.save_state()
        elif ctx.injects:
            print("  Injects already created (resume) — skipping.")

    # Last: the range is prepared and snapshotted, so arm it (implants + the wipe of their
    # traces) and let red attack — or hold, leaving the arming to competition-ops.py start.
    from deploy_lib.phases.final import begin_red_after_delivery
    begin_red_after_delivery(ctx, ctx.state.get("red"))

    # The portal needs Quotient's final event.conf (team logins) and the final box set.
    # Opt-in via Compfile `portal 1`, and it never fails the deploy (portal_ops).
    deploy_portal(ctx)
