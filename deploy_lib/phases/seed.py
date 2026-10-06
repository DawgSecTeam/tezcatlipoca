"""Phase 8: seed teams, unpause the engine, create injects — each once."""

from config_ops import injects_fingerprint, resolve_inject_times
from quotient.setup import create_injects, engine_paused, seed_teams, unpause_engine
from ssh_ops import wait_for_http
from timing import timed


def phase8_seed(ctx):
    """[8/8] Seed teams, unpause the engine, create injects — each once.

    None of the three POSTs is idempotent, so each is guarded by its own state
    flag: a resume in the crash window between a POST and the flag-save must not
    repeat it. The unpause window is the one that asks the engine first
    (engine_paused) because a re-POST there is the visible failure mode."""
    if ctx.from_phase > 8:
        print("[8/8] Skipped (resume).")
        return
    print("[8/8] Seeding competition and creating injects...")

    with timed(ctx.comp_dir, 8, "wait_quotient_http"):
        wait_for_http(f"http://{ctx.scoring_ip}/api/login", timeout=120)

    quotient_ctx = {
        "teams": {team_key: team_data["identifier"] for team_key, team_data in ctx.teams.items()},
        "quotient_admin_password": ctx.admin_password,
    }

    if not ctx.state.get("seeded"):
        print("  Seeding teams and starting the competition clock...")
        with timed(ctx.comp_dir, 8, "seed_teams"):
            seed_teams(ctx.scoring_ip, quotient_ctx)
        ctx.state["seeded"] = True
        ctx.save_state()
    else:
        print("  Teams already seeded (resume) — skipping.")

    if not ctx.state.get("engine_unpaused"):
        # The unpause POST isn't idempotent, so a resume in the crash
        # window between POST and flag-save asks the engine first and
        # re-POSTs only when it really is still paused.
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

    # `injects_created` alone is not a done-marker: it cannot tell "already created"
    # from "already created, but the inject set has since changed". The fingerprint is
    # taken BEFORE resolve_inject_times (which pops the offsets), and a mismatch
    # re-enters create_injects — safe because that call dedups on titles, so only the
    # genuinely new injects are posted.
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
