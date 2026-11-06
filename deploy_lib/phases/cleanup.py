"""Phase 1: reclaim this competition's interrupted clones, then reset the comp dir."""

from pathlib import Path

import providers
from estate import state as estate_state


def reset_domain_markers(comp_dir):
    # Domain done-markers describe THIS deploy's DCs: a prior run's marker makes the
    # domain pass skip promotion on fresh clones. Fresh deploy resets; resumes keep.
    for stale in Path(comp_dir).glob(".nakon-domain-*.json"):
        stale.unlink()


def reclaim_interrupted_clones(ctx):
    providers.of_ctx(ctx).reclaim_interrupted(ctx)


def phase1_cleanup(ctx):
    if ctx.from_phase > 1:
        print("[1/8] Skipped (resume) — keeping the run's markers in place.")
        return
    print("[1/8] Reclaiming interrupted clones and resetting local deploy state...")
    reclaim_interrupted_clones(ctx)
    reset_domain_markers(ctx.comp_dir)
    (ctx.comp_dir / ".postclone-swept").unlink(missing_ok=True)
    # Fresh deploy starts a new teardown cycle: the comp dir mints its run id once,
    # so a surviving journal would skip the NEW range's steps. Resumes keep it.
    if estate_state.state_path(ctx.comp_dir).exists():
        document = estate_state.EstateState.load(ctx.comp_dir)
        document.section("teardown").clear()
        document.save()
