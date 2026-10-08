"""Teardown: destroy API-cloned VMs and `terraform destroy`.

CLI entrypoint only. The mechanics live in destroy_sweep_ops (pre-stop, bounded terraform
destroy with recovery, tagged sweep), destroy_gate_ops (ownership/frozen/endpoint refusals,
checked before any Proxmox mutation) and destroy_templates_ops (golden/jump/engine template
teardown)."""

import json
import os
import sys
from pathlib import Path

import urllib3
from dotenv import load_dotenv

import artifacts_ops
from constants import ownership_tags
from destroy_gate_ops import load_ownership, refuse_frozen_full_teardown, require_terraform_state
from destroy_sweep_ops import destroy_with_recovery, pre_stop_windows_boxes, report_remaining
from destroy_templates_ops import teardown_templates
from nodes_ops import activate_placement, read_placement, record_of
from remote_access_ops import teardown_remote_access
from template_ops import frozen_state
from utils import load_compfile, pick_competition

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)


def load_destroyable_competitions():
    return [
        p.name
        for p in sorted(Path("competitions").iterdir())
        if p.is_dir()
        and (p / "Compfile").exists()
        and (p / "teams.json").exists()
        and (p / "boxes.json").exists()
    ]


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Destroy a deployed competition. Default (teams-only) keeps the "
                    "competition's golden + engine templates for the next test run of "
                    "the SAME competition; --full removes them too. Goldens never "
                    "carry across competitions — tear down --full once the run's "
                    "goal is met.")
    parser.add_argument("--competition", metavar="NAME",
                        help="competition directory under competitions/ (skips the picker)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the type-the-ID confirmation (for scripted teardown)")
    parser.add_argument("--full", action="store_true",
                        help="M4 full teardown: also destroy the golden templates and the "
                             "engine template (after every clone is gone). Default is "
                             "teams-only: templates are kept and the next deploy reuses "
                             "or rebuilds them by hash.")
    parser.add_argument("--end-of-competition", action="store_true", dest="end_of_competition",
                        help="required with --full when the competition is FROZEN — an "
                             "accidental full teardown during the event must be impossible.")
    parser.add_argument("--skip-artifacts", action="store_true", dest="skip_artifacts",
                        help="Skip collecting this run's test artifacts into "
                             "competitions/<id>/.automated-tests/<run-id>/ before destroying. "
                             "Normally collected first, because the red report and the blue "
                             "logs only exist while the boxes do.")
    parser.add_argument("--artifacts-timeout", type=int, default=45, metavar="SEC",
                        dest="artifacts_timeout",
                        help="Per-file timeout for the artifact pull (default 45s). The pull "
                             "never blocks the destroy; it warns and proceeds.")
    args = parser.parse_args()

    print("=" * 64)
    print("  COMPETITION TEARDOWN TOOL")
    print("=" * 64)
    print("Selects a deployed competition and runs terraform destroy to")
    print("remove all provisioned VMs, bridges, and networking.\n")

    competitions = load_destroyable_competitions()
    if not competitions:
        print("No destroyable competitions found.")
        print("(Competitions must have been deployed with create-competition.py")
        print("after teams.json support was added to qualify.)")
        sys.exit()

    if args.competition:
        if args.competition not in competitions:
            print(f"'{args.competition}' is not a destroyable competition. Found: {', '.join(competitions)}")
            sys.exit(1)
        competition = args.competition
    else:
        competition = pick_competition(competitions, label="destroyable", action="Select a competition to destroy")
    if competition is None:
        print("Quitting.")
        sys.exit()

    comp_dir = Path("competitions") / competition
    name, scenario, difficulty = load_compfile(comp_dir / "Compfile")
    teams = json.loads((comp_dir / "teams.json").read_text())
    boxes = json.loads((comp_dir / "boxes.json").read_text())
    frozen = frozen_state(comp_dir)

    # Multi-node: the placement record is authoritative — point the env at the
    # engine's host and register the node routes before anything node-scoped runs.
    placement = read_placement(comp_dir)
    team_nodes = {}
    default_node = os.environ.get("TF_VAR_proxmox_node", "pve")
    placement_nodes = [default_node]
    if placement:
        placement_nodes = [placement["engine_node"]] + [
            record_of(placement, s["name"]).node for s in placement["satellites"]]
    if placement:
        activate_placement(placement)
        team_nodes = placement["team_nodes"]
        sats = ", ".join(f"{s['name']} (slot {s['slot']}, teams {','.join(s['teams'])})"
                         for s in placement["satellites"])
        print(f"  Multi-node placement: engine on '{placement['engine_node']}'"
              + (f"; satellites: {sats}" if sats else ""))

    # The refusal must fire BEFORE any destruction — a frozen competition's templates
    # are the verified artifacts the event runs on.
    refuse_frozen_full_teardown(competition, frozen, args.full, args.end_of_competition)

    mode = "FULL (templates destroyed)" if args.full else "teams-only (templates kept)"
    print("─── About to destroy " + "─" * 42)
    from utils import env_summary
    print(f"  Targeting   : {env_summary()}")
    print(f"  Competition : {name} ({competition})")
    print(f"  Teams       : {len(teams)}")
    print(f"  Boxes       : {len(boxes)} type(s)")
    for b in boxes:
        print(f"    {b['name']} — {b['template']}")
    print(f"  Mode        : {mode}"
          + ("  [FROZEN — end-of-competition flag present]" if frozen and args.end_of_competition else ""))
    # .deploy_state.json is read ONCE: it carries the deploy's run id (the
    # destruction-ownership anchor) and the engine vmid, plus the endpoint guard.
    deployed_state, run_id = load_ownership(comp_dir)
    require_terraform_state(comp_dir, deployed_state)
    print(f"  Ownership: run id '{run_id}' — only VMs carrying this deploy's FULL "
          f"tag set (tezcatlipoca + comp-{competition} + {run_id}) will be touched.")
    print()
    print("  This will run: terraform destroy -parallelism=4 -auto-approve")
    if args.full:
        print("  Then the golden templates and the engine template are destroyed "
              "(clones first — their base disks depend on them).")
    else:
        print("  The golden templates and the engine template are KEPT (M4 teams-only): "
              "the next deploy reuses them by hash.")
    print()

    if args.yes:
        print(f"  --yes: skipping confirmation for '{competition}'.")
    else:
        confirm = input(f"  Type the competition ID to confirm ({competition}): ").strip()
        if confirm != competition:
            print("Cancelled — nothing was destroyed.")
            sys.exit()

    env = {**os.environ}
    env["TF_VAR_teams"] = json.dumps(teams)
    env["TF_VAR_boxes_per_team"] = json.dumps(boxes)
    env["TF_VAR_event_name"] = name

    # ── Collect this run's test artifacts — BEFORE anything is stopped or deleted ──────────
    # Last chance by construction: the red report and the blue logs live on machines that are
    # about to be destroyed, and the calls immediately below end that (pre_stop_windows_boxes
    # hard-stops every team box, after which a stopped
    # guest's agent can no longer answer). This is also the safety net for a run whose harness
    # died: teardown is the one script AGENTS.md tells you to re-run until clean, so it is the
    # only step guaranteed to happen. It warns and proceeds — a dead box must never wedge a
    # teardown — and records what it could not get in collection.json / REPORT.md.
    if args.skip_artifacts:
        print("\n  Artifacts  : skipped (--skip-artifacts)")
    else:
        print("\nCollecting test artifacts before teardown...")
        try:
            artifacts_ops.collect_for_teardown(
                comp_dir, run_id=run_id, teams=teams, boxes=boxes,
                node=default_node, nodes=placement_nodes,
                script="destroy-competition.py", timeout=args.artifacts_timeout)
        except Exception as e:  # never let bookkeeping outrank destroying the range
            print(f"  WARNING: artifact collection failed ({type(e).__name__}: {e}) — "
                  "continuing with the teardown; nothing was destroyed by this step.")
        print()

    pre_stop_windows_boxes(teams, boxes, default_node, team_nodes=team_nodes,
                           expect_tags=ownership_tags(competition, run_id))

    # Destroy from this competition's own per-comp state dir, so we tear down only this
    # comp's engine/boxes/bridges.
    tf_cwd = str(comp_dir / "terraform")

    print(f"\nRunning terraform destroy for '{name}' (state: {tf_cwd})...")
    if not destroy_with_recovery(env, tf_cwd, placement_nodes, competition,
                                 run_id=run_id):
        report_remaining(placement_nodes, competition, teams, run_id=run_id)
        sys.exit("ERROR: terraform destroy did not complete after recovery attempts — "
                 "resolve what is listed above, then re-run this command (it is safe "
                 "to re-run: teardown is idempotent).")

    # M3: the golden templates are API-created (not in terraform state) and are the
    # linked clones' base disks — they die only after terraform destroy removed every
    # clone.
    teardown_templates(comp_dir, competition, run_id, boxes, deployed_state, placement,
                       full=args.full)

    # Headscale remote access: revoke the engine's tailnet node (kills its node key),
    # delete this comp's participant users, re-apply the policy without this comp.
    # Warn-and-proceed like the rest of teardown — a dead headscale host must never
    # block destroying a range.
    try:
        teardown_remote_access(comp_dir, state=deployed_state)
    except Exception as e:
        print(f"  WARNING: headscale remote-access cleanup failed "
              f"({type(e).__name__}: {e}) — continuing.")

    print(f"\nInfrastructure for '{competition}' destroyed.")
    print(f"Competition files preserved at competitions/{competition}/")


if __name__ == "__main__":
    main()
