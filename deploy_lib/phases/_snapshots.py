"""The two restore-point snapshots (tz-base before the sweep, tz-ready as delivered)."""

from constants import SNAP_BASE, SNAP_READY
from range_ops import take_snapshot
from timing import timed


def snap_base(ctx, t, phase=4):
    """Take the pre-sweep SNAP_BASE restore point on one target (the run_concurrent unit).
    Phase 4 takes the managed boxes'; phase 5 takes the firewalls' after the cutover."""
    with timed(ctx.comp_dir, phase, "snapshot", t["vm_name"]):
        take_snapshot(t.get("node", ctx.node), t["vmid"], SNAP_BASE,
                      description="tezcatlipoca: booted, networked, pre-sweep")


def snap_ready(ctx, t):
    """Take the as-delivered SNAP_READY restore point on one target."""
    with timed(ctx.comp_dir, 7, "snapshot", t["vm_name"]):
        take_snapshot(t.get("node", ctx.node), t["vmid"], SNAP_READY,
                      description="tezcatlipoca: as delivered, post-sweep + hardening")
