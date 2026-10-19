"""Phase 8: seed teams, unpause the engine, create injects (each once), then the opt-in portal."""

from config.injects_config import injects_fingerprint, resolve_inject_times
from portal.portal_ops import deploy_portal
from quotient.setup import create_injects, engine_paused, seed_teams, unpause_engine
from common.ssh_ops import wait_for_http
from common.timing import timed


def phase8_seed(ctx):
    if ctx.from_phase > 8:
        print("[8/8] Skipped (resume).")
        return
    held = bool(ctx.state.get("hold_start"))
    print("[8/8] Seeding competition and creating injects...")

    with timed(ctx.comp_dir, 8, "wait_quotient_http"):
        wait_for_http(f"http://{ctx.scoring_ip}/api/login", timeout=120)

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

    # Last: the portal needs Quotient's final event.conf (team logins) and the final box
    # set. Opt-in via Compfile `portal 1`, and it never fails the deploy (portal_ops).
    deploy_portal(ctx)
