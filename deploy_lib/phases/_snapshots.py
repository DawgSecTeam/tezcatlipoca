"""The restore points (tz-base before the sweep, tz-ready on a prepared range)."""

from functools import partial

import providers

from common.constants import SNAP_BASE, SNAP_READY
from common.timing import timed
from common.utils import run_concurrent
from infra.lifecycle import list_snapshots


def snap_base(ctx, t, phase=4):
# Take the pre-sweep SNAP_BASE restore point on one target (the run_concurrent unit).
#     Phase 4 takes the managed boxes'; phase 5 takes the firewalls' after the cutover.
    with timed(ctx.comp_dir, phase, "snapshot", t["vm_name"]):
        providers.of_ctx(ctx).restore_point_create(
            ctx, t, SNAP_BASE,
            description="tezcatlipoca: booted, networked, pre-sweep")


def snap_ready(ctx, t):
# Take the prepared SNAP_READY restore point on one target: built, not yet compromised.
    with timed(ctx.comp_dir, 8, "snapshot", t["vm_name"]):
        providers.of_ctx(ctx).restore_point_create(
            ctx, t, SNAP_READY,
            description="tezcatlipoca: built and hardened, implants not deployed")


def snap_red(ctx, red_summary):
# red01's prepared point: the C2 and its payload matrix exist, no implant is deployed yet.
#     A round resets here, so the re-arm reuses the matrix instead of freezing it again.
    if not red_summary:
        return
    with timed(ctx.comp_dir, 8, "snapshot", "red01"):
        providers.of_ctx(ctx).restore_point_create(
            ctx, {"vm_name": "red01", "vmid": red_summary["vmid"],
                  "node": red_summary["node"]},
            SNAP_READY,
            description="tezcatlipoca: C2 + payload matrix built, implants not deployed")


def snap_engine(ctx):
# The engine's prepared point: event.conf, credlist and seeded teams in place, clock stopped
#     and no inject yet, so a reset re-anchors the event without re-cloning the VM.
    with timed(ctx.comp_dir, 8, "snapshot", "engine"):
        providers.of_ctx(ctx).restore_point_create(
            ctx, {"vm_name": "engine", "vmid": ctx.engine_vmid,
                  "node": ctx.node},
            SNAP_READY,
            description="tezcatlipoca: engine prepared, teams seeded, clock stopped")


def snap_prepared(ctx):
# One prepared point for the whole range: every box, red01 and the engine, at one moment.
#     Taken once — a resume must never capture a disk an earlier attempt already armed.
    node = ctx.node
    fresh = [t for t in ctx.all_targets
             if SNAP_READY not in list_snapshots(t.get("node") or node, t["vmid"])]
    if fresh:
        print(f"  Snapshotting {len(fresh)} box(es) as '{SNAP_READY}' (prepared)...")
        run_concurrent(fresh, partial(snap_ready, ctx), max_workers=4)
    red = ctx.state.get("red") or {}
    if red.get("vmid") and SNAP_READY not in list_snapshots(
            red.get("node") or node, int(red["vmid"])):
        snap_red(ctx, red)
    if SNAP_READY not in list_snapshots(node, ctx.engine_vmid):
        snap_engine(ctx)
