"""Phase 5: bootstrap the in-path firewalls, then cut the engine over."""

from functools import partial

from constants import SNAP_BASE
from firewall_ops import (bootstrap_firewalls, cut_over_engine, verify_in_path,
                          write_team_configs)
from timing import timed
from utils import compfile_value, is_in_path_fw, run_concurrent

from deploy_lib.phases._snapshots import snap_base


def phase5_firewall_bootstrap(ctx):
    """[5/8] Bootstrap the in-path firewalls, then cut the engine over.

    Runs only when the lineup declares an `in_path` box (an unmanaged firewall): every
    team's pfSense (a clone of the `pfsense-provision` template) gets its per-team
    config.xml pushed over SSH through the engine, then the engine cutover moves the gateway address onto the firewalls and points the engine's
    routes through the transit /30s. From here on every engine→box path (repair sweep,
    final pass, beacons, scoring) is verified to run THROUGH the firewall.

    No firewalls in the lineup: print-and-return — the phase index stays stable, so
    checkpoints and resume banners never depend on the box lineup.

    Resumable: a --from-phase 5 re-pushes every config (a firewall whose config already
    matches is left alone, no reboot) and re-runs the cutover (the netplan rewrite is
    idempotent); the completion flag is written for reporting, not for skipping."""
    if ctx.from_phase > 5:
        print("[5/8] Skipped (resume).")
        return
    fw_targets = [t for t in ctx.all_targets if is_in_path_fw(t["box"])]
    if not fw_targets:
        print("[5/8] No in-path firewall in this lineup — skipping.")
        return
    print("[5/8] Bootstrapping the in-path firewalls (SSH config push → reboot → "
          "engine cutover)...")
    if ctx.placement and ctx.placement["satellites"]:
        raise SystemExit(
            "  ERROR: in-path firewalls are engine-node (slot 0) only — preflight should "
            "have refused this lineup. Refusing to bootstrap against a satellite placement.")
    dnat = compfile_value(ctx.comp_dir / "Compfile", "firewall_dnat")
    red_dnat_spec = [s.strip() for s in dnat.split(",") if s.strip()] or None
    with timed(ctx.comp_dir, 5, "firewall_configs", f"x{len(fw_targets)}"):
        config_paths = write_team_configs(ctx.comp_dir, ctx.teams,
                                          red_dnat_spec=red_dnat_spec)
    with timed(ctx.comp_dir, 5, "firewall_bootstrap", f"x{len(fw_targets)}"):
        bootstrap_firewalls(ctx.teams, fw_targets, config_paths, ctx.tf_ctx)
    with timed(ctx.comp_dir, 5, "engine_cutover"):
        cut_over_engine(ctx.tf_ctx, ctx.teams)
    # The first managed box of each team (boxes.json order) proves the routed path a
    # plant/scoring step will take — probe it through the firewall.
    first_box = {}
    for t in ctx.all_targets:
        if not is_in_path_fw(t["box"]):
            first_box.setdefault(t["team_key"], t["ip"])
    with timed(ctx.comp_dir, 5, "verify_in_path"):
        verify_in_path(ctx.tf_ctx, ctx.teams, first_box)
    # The firewalls' tz-base lands HERE, after the cutover — a pre-cutover restore
    # point would capture a firewallless network pretending to be in-path.
    print(f"  Snapshotting the firewalls as '{SNAP_BASE}' (post-cutover restore point)...")
    run_concurrent(fw_targets, partial(snap_base, ctx, phase=5), max_workers=4)
    ctx.state["firewalls_bootstrapped"] = True
    ctx.save_state()
