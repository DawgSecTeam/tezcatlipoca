"""The create-competition.py command line: argument parsing, competition scaffolding, --plan-only."""

import argparse
import json
import sys
from pathlib import Path

from config_ops import (_prompt_difficulty, collect_boxes, collect_users_config,
                        load_previous_competitions)
from utils import valid_comp_name

from deploy_lib.runner import deploy


def build_parser():
    parser = argparse.ArgumentParser(
        description="Tezcatlipoca — CTF Range Deployment. With no flags it runs fully "
                    "interactively (as before); flags let it run non-interactively.",
    )
    parser.add_argument("--competition", help="Competition name. If it already has a Compfile + "
                                              "boxes.json, deploy it straight away; otherwise it "
                                              "is created (needs --scenario/--difficulty or falls "
                                              "back to prompting).")
    parser.add_argument("--teams", type=int, help="Number of teams (skips the 'How many teams?' prompt).")
    parser.add_argument("--yes", action="store_true", help="Skip the confirm-deploy prompt.")
    parser.add_argument("--scenario", help="Scenario description (only when creating a new competition).")
    parser.add_argument("--difficulty", type=int, help="Difficulty 1-10 (only when creating a new competition).")
    parser.add_argument("--box-username", dest="box_username",
                        help="Themeable box login username (only when creating a new competition; "
                             "default 'ubuntu'). Written to competitions/<id>/users.json.")
    parser.add_argument("--credlist-usernames", dest="credlist_usernames",
                        help="Comma-separated, exactly 3 themeable credlist account names (only "
                             "when creating a new competition; default 'admin,user1,user2'). "
                             "Written to competitions/<id>/users.json.")
    parser.add_argument("--from-phase", type=int, default=1, dest="from_phase",
                        help="Resume from this phase (>1 skips the destructive cleanup + terraform "
                             "apply). See the resume hint printed on a failed deploy. Refused when "
                             "it skips phases the state file doesn't record as completed.")
    parser.add_argument("--force-from-phase", action="store_true", dest="force_from_phase",
                        help="Proceed even when --from-phase skips phases .deploy_state.json does "
                             "not record as completed. Only for a known-stale checkpoint (e.g. the "
                             "process died after a phase finished but before it was checkpointed); "
                             "the skipped phases build the machines the later ones target.")
    parser.add_argument("--min-load-free", type=float, default=None, dest="min_load_free",
                        help="On a resume into a phase that previously failed, wait until the "
                             "node's 1-minute load is below this before starting. Phase-4 retries "
                             "used to be hand-throttled this way; a contended node, not a short "
                             "timeout, is what made cde-2026 loop eleven times.")
    parser.add_argument("--scoring-vmid", type=int, default=None, dest="scoring_vmid",
                        help="VMID for this competition's scoring engine (default 1000). Give each "
                             "concurrent competition on a shared node a distinct free VMID so their "
                             "engines don't collide. Persisted to .deploy_state.json and reused on "
                             "resume (ignored on --from-phase, which reads it back from state).")
    parser.add_argument("--team-node", dest="team_node", default=None,
                        help="Multi-node pin: comma list of team=NODE, e.g. '103=zfs-193,104=zfs-193' "
                             "(team key or subnet identifier = nodes.json node name). Unpinned teams "
                             "are placed by capacity-fill. Needs nodes.json; ignored when this "
                             "competition already has a placement.json.")
    parser.add_argument("--engine-node", dest="engine_node", default=None,
                        help="Multi-node pin: which nodes.json node hosts the scoring engine "
                             "(default: the node holding the most teams). Needs nodes.json; ignored "
                             "when this competition already has a placement.json.")
    parser.add_argument("--plan-only", action="store_true", dest="plan_only",
                        help="Collect/generate the competition's config (Compfile, boxes.json) "
                             "and print a summary, then exit WITHOUT touching any infrastructure "
                             "— no teardown, no `terraform apply`. There is no confirmation "
                             "checkpoint between the box picker and a real deploy otherwise "
                             "(--yes skips it outright, and piped/scripted stdin that happens to "
                             "satisfy every remaining prompt walks straight into one) — use this "
                             "to review the plan first, then re-run without the flag to deploy it "
                             "for real.")
    return parser


def print_plan(comp_name, comp_dir):
    """--plan-only: summarize the competition's boxes and run identity; touch nothing."""
    boxes = json.loads((comp_dir / "boxes.json").read_text())
    print(f"\n  ── PLAN for '{comp_name}' — nothing has been deployed " + "─" * 20)
    for b in boxes:
        disk = f"{b['disk_gb']} GB disk" if b.get("disk_gb") else "template's own disk"
        kind = "  (in-path firewall: vmbrW<id> → fw → team bridge, gateway .1)" \
            if b.get("in_path") else "  (unmanaged appliance)" if b.get("unmanaged") else ""
        print(f"    {b['name']:<12} {b['template']:<20} {b['cpu']} CPU, {b['memory_mb']} MB, {disk}{kind}")
    try:
        state_run = (json.loads((comp_dir / ".deploy_state.json").read_text()).get("run_id") or "")
    except (ValueError, OSError):
        state_run = ""
    if state_run:
        print(f"  Run identity: {state_run} (reused from this competition's state)")
    else:
        print("  Run identity: minted at deploy time (run-<id>, stored in "
              ".deploy_state.json) — destruction paths require it")
    print("\n  Team count is decided at deploy time (--teams N, or the prompt).")
    print("  Nothing was deployed — no teardown, no terraform apply.")
    print(f"  Deploy for real with: python3 create-competition.py --competition {comp_name} "
          f"--teams <N> --yes")


def write_new_competition(comp_dir, comp_name, scenario, difficulty,
                          box_username_flag=None, credlist_flag=None):
    """Write a new competition's Compfile, boxes.json (interactive picker) and users.json."""
    (comp_dir / "Compfile").write_text(
        f"name {comp_name}\n"
        f"scenario {scenario}\n"
        f"difficulty {difficulty}\n"
    )
    boxes = collect_boxes()
    (comp_dir / "boxes.json").write_text(json.dumps(boxes, indent=2))
    box_username, credlist_usernames = collect_users_config(
        box_username_flag=box_username_flag, credlist_flag=credlist_flag
    )
    (comp_dir / "users.json").write_text(json.dumps(
        {"box_username": box_username, "credlist_usernames": credlist_usernames}, indent=2
    ))


def _checked_comp_name(raw):
    comp_name = raw.strip().lower().replace(" ", "-")
    if not valid_comp_name(comp_name):
        sys.exit(f"  ERROR: invalid competition name {comp_name!r} — use [a-z0-9._-], no path separators.")
    return comp_name


def select_named_competition(args):
    """--competition NAME: reuse it when it has a Compfile + boxes.json, else scaffold it."""
    comp_name = _checked_comp_name(args.competition)
    comp_dir = Path("competitions") / comp_name
    has_compfile = (comp_dir / "Compfile").exists()
    has_boxes = (comp_dir / "boxes.json").exists()

    if comp_dir.is_dir() and has_compfile and has_boxes:
        print(f"Reusing existing competition '{comp_name}'.")
    else:
        print(f"Creating new competition '{comp_name}'.")
        comp_dir.mkdir(parents=True, exist_ok=True)
        scenario = args.scenario if args.scenario is not None else input("Scenario description: ").strip()
        difficulty = args.difficulty if args.difficulty is not None else _prompt_difficulty()
        write_new_competition(comp_dir, comp_name, scenario, difficulty,
                              box_username_flag=args.box_username,
                              credlist_flag=args.credlist_usernames)
    return comp_name, comp_dir


def select_interactive_competition():
    """No --competition: list the previous ones and prompt for new-or-reuse."""
    previous = load_previous_competitions()
    if previous:
        print("\nPrevious competitions:")
        for i, name in enumerate(previous, 1):
            print(f"  [{i}] {name}")
        print()

    choice = input("Create new (n) or reuse existing (number)? ").strip()

    if choice.lower() == "n":
        comp_name = _checked_comp_name(input("Competition name: "))
        comp_dir = Path("competitions") / comp_name
        comp_dir.mkdir(parents=True, exist_ok=True)

        scenario = input("Scenario description: ").strip()
        difficulty = _prompt_difficulty()
        write_new_competition(comp_dir, comp_name, scenario, difficulty)
    else:
        try:
            idx = int(choice) - 1
        except ValueError:
            print("Invalid choice.")
            sys.exit(1)
        if 0 <= idx < len(previous):
            comp_name = previous[idx]
        else:
            print("Invalid choice.")
            sys.exit(1)
        comp_dir = Path("competitions") / comp_name
    return comp_name, comp_dir


def main():
    args = build_parser().parse_args()

    print("Tezcatlipoca - CTF Range Deployment")
    print("=" * 40)

    if args.competition:
        comp_name, comp_dir = select_named_competition(args)
    else:
        comp_name, comp_dir = select_interactive_competition()

    if args.plan_only:
        print_plan(comp_name, comp_dir)
        return

    deploy(comp_dir, num_teams=args.teams, assume_yes=args.yes, from_phase=args.from_phase,
           scoring_vmid=args.scoring_vmid, team_node=args.team_node, engine_node=args.engine_node,
           force_from_phase=args.force_from_phase, min_load_free=args.min_load_free)
