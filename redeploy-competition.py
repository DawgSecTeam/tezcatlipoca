"""Redeploy a subset of a live competition's boxes (rollback-ready/base, reconfigure, rebuild).

CLI entrypoint only: argument parsing, the confirmation flow and mode dispatch. The modes
live in redeploy_light_ops (resync / rollback / reconfigure), redeploy_rebuild_ops,
redeploy_reset_ops and redeploy_engine_ops; target selection in redeploy_select_ops, the
shared configure half in redeploy_plant_ops, and the state/run-id gate (checked before any
Proxmox call) in redeploy_gate_ops."""

import argparse
import os
from pathlib import Path

import urllib3
from dotenv import load_dotenv

import pipeline_api
from constants import SNAP_BASE, SNAP_READY
from nakon_ops import acquire_engine_lock, release_engine_lock
from range_ops import describe_target, list_snapshots, parse_vm_tags, proxmox_api, snapshot_support_hint
from redeploy_engine_ops import engine_recovery, reseed_event
from redeploy_gate_ops import (deployed_candidates, load_deployed_range,
                               validate_comp_name)
from redeploy_light_ops import mode_reconfigure, mode_resync, mode_rollback
from redeploy_plant_ops import prepare_nakon_assets
from redeploy_rebuild_ops import mode_rebuild
from redeploy_reset_ops import mode_reset
from redeploy_select_ops import select_targets
from utils import load_compfile, pick_competition

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def main():
    parser = argparse.ArgumentParser(
        description="Redeploy a subset of a live competition's boxes without tearing down the "
                    "range. Selection flags combine with AND; with none given, every box in "
                    "the competition is selected.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # one team's boxes, back to the state the competition started in
  redeploy-competition.py --competition cde-2026 --teams 3

  # one box for one team
  redeploy-competition.py --competition cde-2026 --teams 3 --boxes web01

  # one team's linux boxes, rebuilt from tz-base and re-planted by Nakon
  redeploy-competition.py --competition cde-2026 --teams 3 --platform linux \\
      --mode rollback-base

  # see what would be touched, change nothing
  redeploy-competition.py --competition cde-2026 --teams 3 --dry-run

  # one box, cheapest reset that works (rollback -> replant -> rebuild as needed)
  redeploy-competition.py --competition cde-2026 --teams 3 --boxes web01 --mode reset
""",
    )
    parser.add_argument("--competition", help="Competition ID (competitions/<id>). Omit for a "
                                              "menu of deployed competitions.")
    parser.add_argument("--teams", help="Comma-separated teams: 'team2', '2' or the subnet "
                                        "identifier '102' all select the same team.")
    parser.add_argument("--boxes", help="Comma-separated box names from boxes.json, e.g. "
                                        "'web01,db01'.")
    parser.add_argument("--platform", choices=["linux", "windows"],
                        help="Only boxes whose template is this platform.")
    parser.add_argument("--mode", default="rollback-ready",
                        choices=["rollback-ready", "rollback-base", "reconfigure", "rebuild",
                                 "resync", "engine-recovery", "reset"],
                        help="What to do to the selected boxes (default: rollback-ready). "
                             "resync = align credentials with the engine and re-set box "
                             "passwords via the guest agent, touching nothing else. "
                             "engine-recovery = re-clone the engine VM from the engine "
                             "template (fresh empty scoring DB; re-seed with "
                             "--from-phase 7); ignores box selection flags. "
                             "reset = cheapest-that-works ladder per box: tz-ready "
                             "rollback, then tz-base rollback + replant, then golden "
                             "rebuild — escalating only the boxes each rung leaves "
                             "unhealthy, and reporting what each box ended up with.")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Print the resolved targets and their snapshots, then exit.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument("--reset-event", action="store_true", dest="reset_event",
                        help="With rollback-ready/rollback-base: also restart the event from the "
                             "engine template (fresh scoring DB) and re-run phase 7, so scores "
                             "reset and injects re-open anchored at now. Use for scrim reruns.")
    args = parser.parse_args()

    comp_name = args.competition
    validate_comp_name(comp_name)
    if not comp_name:
        candidates = deployed_candidates()
        if not candidates:
            raise SystemExit("No deployed competitions found (need Compfile + teams.json).")
        comp_name = pick_competition(candidates, label="deployed",
                                     action="Select a competition to redeploy from")
        if comp_name is None:
            raise SystemExit("Quitting.")

    comp_dir = Path("competitions") / comp_name
    if not comp_dir.is_dir():
        raise SystemExit(f"  ERROR: no such competition: {comp_dir}")

    name, _scenario, difficulty = load_compfile(comp_dir / "Compfile")
    teams, boxes, state, state_path = load_deployed_range(comp_dir)

    # Multi-node: the placement record is authoritative — env -> engine host and
    # node routes registered before anything node-scoped (snapshots, clones).
    from nodes_ops import activate_placement, read_placement
    placement = read_placement(comp_dir)
    if placement:
        activate_placement(placement)

    if args.mode == "engine-recovery":
        engine_recovery(name, comp_dir, teams, boxes, state, assume_yes=args.yes)
        return
    if args.reset_event and args.mode not in ("rollback-ready", "rollback-base", "reset"):
        raise SystemExit("  ERROR: --reset-event only applies to rollback-ready/rollback-base/reset.")

    targets = select_targets(comp_dir, teams, boxes, args)
    if not targets:
        raise SystemExit("  No boxes matched the given filters — nothing to do.")

    node = os.environ["TF_VAR_proxmox_node"]

    print(f"\n{'='*64}")
    print(f"  Redeploy — {name} ({comp_name})")
    print(f"{'='*64}")
    print(f"  Mode: {args.mode}")
    print(f"  {len(targets)} of {len(teams) * len(boxes)} box(es) selected:\n")
    snaps_by_box = {}
    for t in targets:
        tnode = t.get("node") or node
        snaps = list_snapshots(tnode, t["vmid"])
        snaps_by_box[t["vm_name"]] = snaps
        have = ", ".join(sorted(snaps)) if snaps else "none"
        try:
            cfg = proxmox_api("GET", f"/nodes/{tnode}/qemu/{t['vmid']}/config")["data"]
            tags = ", ".join(sorted(parse_vm_tags(cfg.get("tags")))) or "UNTAGGED"
        except Exception:
            tags = "?"
        print(f"    {describe_target(t)}  [snapshots: {have}]  [tags: {tags}]")
    print()

    if args.mode == "reset":
        print("  reset ladder — cheapest rung each box would start at:")
        for t in targets:
            snaps = snaps_by_box[t["vm_name"]]
            if SNAP_READY in snaps:
                first = f"1/3 ('{SNAP_READY}' rollback)"
            elif SNAP_BASE in snaps:
                first = f"2/3 ('{SNAP_BASE}' rollback + replant)"
            else:
                first = "3/3 golden rebuild — no usable snapshots"
            print(f"    {t['team_key']}/{t['box_name']}: rung {first}")
        print()

    if args.dry_run:
        print("  --dry-run: nothing was changed.")
        return

    needed = {"rollback-ready": SNAP_READY, "rollback-base": SNAP_BASE}.get(args.mode)
    if needed:
        missing = [t for t in targets if needed not in list_snapshots(t.get("node") or node, t["vmid"])]
        if missing:
            print(f"  ERROR: {len(missing)} selected box(es) have no '{needed}' snapshot:")
            for t in missing:
                print(f"    {describe_target(t)}")
            print("\n  " + snapshot_support_hint(node, missing[0]["vmid"]))
            raise SystemExit(1)

    if not args.yes:
        if args.mode not in ("reconfigure", "resync"):
            print("  This DISCARDS everything the defending team(s) have done to these boxes.")
        answer = input(f"  Redeploy {len(targets)} box(es) in mode '{args.mode}'? [y/N] ").strip()
        if answer.lower() not in ("y", "yes"):
            print("  Cancelled — nothing was changed.")
            return

    acquire_engine_lock(int(state.get("scoring_vm_id") or 1000))
    ctx = pipeline_api.read_terraform_ctx(comp_dir)

    nakon_config_path = nakon_bundle = None
    if args.mode in ("rollback-base", "reconfigure", "rebuild"):
        # mode_reset prepares these lazily — a reset that settles at the tz-ready rung
        # must not demand stage files it would never have used.
        nakon_config_path, nakon_bundle = prepare_nakon_assets(comp_dir, boxes)

    if args.mode == "rollback-ready":
        done = mode_rollback(targets, ctx, node, SNAP_READY, comp_dir, state,
                             nakon_config_path, nakon_bundle, reconfigure=False)
    elif args.mode == "rollback-base":
        done = mode_rollback(targets, ctx, node, SNAP_BASE, comp_dir, state,
                             nakon_config_path, nakon_bundle, reconfigure=True)
    elif args.mode == "reconfigure":
        done = mode_reconfigure(targets, ctx, comp_dir, state, nakon_config_path, nakon_bundle)
    elif args.mode == "resync":
        done = mode_resync(targets, ctx, node, comp_dir, state, state_path)
    elif args.mode == "reset":
        done = mode_reset(targets, ctx, node, comp_dir, state, teams, boxes, difficulty)
    else:
        done = mode_rebuild(targets, ctx, node, comp_dir, state, nakon_config_path, nakon_bundle)

    if args.reset_event:
        print("\n  --reset-event: restarting the event from the engine template...")
        if engine_recovery(name, comp_dir, teams, boxes, state, assume_yes=True):
            release_engine_lock()  # the reseed child takes the engine lock itself
            reseed_event(comp_dir)

    print(f"\n{'='*64}")
    print(f"  Redeployed {len(done)} box(es) in mode '{args.mode}'")
    print(f"{'='*64}")
    for t in done:
        print(f"    {describe_target(t)}")
    print(f"\n  Verify with: python3 verify-competition.py competitions/{comp_name}")
    print(f"  Scoreboard:  http://{ctx['scoring_engine_ip']}")


if __name__ == "__main__":
    main()
