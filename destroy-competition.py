"""Teardown CLI: a thin shim over the teardown package (docs/internals.md#teardown).

The mechanics, the step policies, the ordering rule and the exit contract live
in teardown/ — this file only parses flags, builds the context, and turns the
engine's verdict into an exit code. Exit 0 means verify_clean found the estate
clean for this ownership set; every refusal is a Refusal (saw / expected /
remedy) printed before anything is touched.

Default mode is a FULL teardown: exit 0 leaves nothing of this deploy behind.
--teams-only keeps the competition's golden + engine templates for the next run
of the SAME competition (the mid-event mode); goldens never carry across
competitions. A FROZEN competition's templates require --end-of-competition.
--team NAME is the mid-event team removal (teardown/team_ops.py): one team's
clones die, the rest of the range is never addressed.
"""

import json
import os
import sys
from pathlib import Path

import urllib3
from dotenv import load_dotenv

from teardown import Refusal
from engine.template_freeze import frozen_state
from common.comp_paths import comps_root, resolve_comp_dir
from common.utils import load_compfile, pick_competition

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)


def load_destroyable_competitions():
    return [
        p.name
        for p in sorted(comps_root().iterdir())
        if p.is_dir()
        and (p / "Compfile").exists()
        and (p / "teams.json").exists()
        and (p / "boxes.json").exists()
    ]


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Destroy a deployed competition. Default is a FULL teardown "
                    "(golden + engine templates included) — exit 0 means nothing of "
                    "this deploy is left. --teams-only keeps the templates for the "
                    "next run of the SAME competition; goldens never carry across "
                    "competitions. A FROZEN competition requires "
                    "--end-of-competition.")
    parser.add_argument("--competition", metavar="NAME",
                        help="competition directory under competitions/ (skips the picker)")
    parser.add_argument("--comp-dir", dest="comp_dir", default=None,
                        help="path to the competition's dir (wins over --competition "
                             "resolution under the comps root)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the type-the-ID confirmation (for scripted teardown)")
    parser.add_argument("--teams-only", action="store_true", dest="teams_only",
                        help="mid-event mode: destroy the team boxes but KEEP the "
                             "golden + engine templates (the next deploy reuses them "
                             "by hash)")
    parser.add_argument("--end-of-competition", action="store_true", dest="end_of_competition",
                        help="required when the competition is FROZEN — an accidental "
                             "teardown of the verified templates mid-event must be "
                             "impossible")
    parser.add_argument("--keep-red", action="store_true", dest="keep_red",
                        help="do not destroy bad-auto's red01 (default: destroy it — it is "
                             "not estate-managed and survives the range otherwise)")
    parser.add_argument("--artifacts-timeout", type=int, default=45, metavar="SEC",
                        dest="artifacts_timeout",
                        help="Per-file timeout for the artifact pull (default 45s). The pull "
                             "never blocks the destroy; it warns and proceeds.")
    parser.add_argument("--team", metavar="TEAM", default=None,
                        help="mid-event team removal: destroy THIS team's clones, deactivate "
                             "it in Quotient and revoke its logins — the rest of the range "
                             "(other teams, engine, templates) is never addressed. Refuses "
                             "anything not owned by this deploy's run id.")
    args = parser.parse_args()

    print("=" * 64)
    print("  COMPETITION TEARDOWN")
    print("=" * 64)
    print("Evidence is collected first; then every VM carrying this deploy's")
    print("full ownership set is force-stopped and destroyed.\n")

    if args.comp_dir:
        comp_dir = resolve_comp_dir(comp_dir=args.comp_dir)
        # `competition` is only bound by the comps-root branch below, and the banner prints it —
        # `--comp-dir` alone (the dashboard's shape: a per-run workspace with no name of its own)
        # died with UnboundLocalError before any teardown ran (2026-10-14, run-eab53563).
        competition = args.competition or comp_dir.name
    else:
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

        comp_dir = resolve_comp_dir(competition)
    name, scenario, difficulty = load_compfile(comp_dir / "Compfile")
    # teams.json/boxes.json are what this teardown plans against. A workspace whose deploy died
    # before generating them (preflight-placement) never held a range, and reading them
    # unconditionally died with FileNotFoundError instead of a verdict (2026-10-14, runs
    # run-0c8d7533 / run-1194ddcf, both handed run-c84faaac's config-less workspace).
    missing = [f for f in ("teams.json", "boxes.json") if not (comp_dir / f).exists()]
    if missing:
        refusal = Refusal(
            saw=f"{comp_dir} has no {', '.join(missing)}",
            expected="the teams.json + boxes.json a deploy generates once its range is built",
            remedy="destroy the workspace that owns the range; this one never held one")
        print(f"\n  REFUSED — {refusal}")
        sys.exit(1)
    teams = json.loads((comp_dir / "teams.json").read_text())
    boxes = json.loads((comp_dir / "boxes.json").read_text())
    frozen = frozen_state(comp_dir)

    if args.team:
        from teardown.team_ops import run_team_teardown
        print("─── Deleting team " + "─" * 46)
        print(f"  Competition : {name} ({competition})")
        print(f"  Team        : {args.team}")
        print("  Only this team's clones are touched; the engine, templates and other")
        print("  teams are never addressed.\n")
        try:
            report = run_team_teardown(comp_dir, args.team, name=name, env={**os.environ})
        except Refusal as r:
            print(f"\n  REFUSED — {r}")
            sys.exit(1)
        print("\n─── Result " + "─" * 52)
        for problem in report["problems"]:
            print(f"  PROBLEM: {problem}")
        if not report["clean"]:
            sys.exit(1)
        print(f"Team '{args.team}' removed; the rest of the range is untouched.")
        return

    full = not args.teams_only
    mode = ("FULL (templates destroyed)" if full
            else "teams-only (templates kept)")
    print("─── About to destroy " + "─" * 42)
    from common.utils import env_summary
    print(f"  Targeting   : {env_summary()}")
    print(f"  Competition : {name} ({competition})")
    print(f"  Teams       : {len(teams)}")
    print(f"  Boxes       : {len(boxes)} type(s)")
    for b in boxes:
        print(f"    {b['name']} — {b['template']}")
    print(f"  Mode        : {mode}"
          + ("  [FROZEN — end-of-competition flag present]" if frozen and args.end_of_competition else ""))
    print()
    print("  This will destroy this run's recorded VMs in tier order — team boxes and")
    print("  jumps, then the golden set, then the engine template and engine, then the")
    print("  bridges (goldens are the linked clones' base disks, so the clones go first).")

    def confirm(comp_name):
        if args.yes:
            print(f"\n  --yes: skipping confirmation for '{comp_name}'.")
            return True
        typed = input(f"\n  Type the competition ID to confirm ({comp_name}): ").strip()
        if typed != comp_name:
            print("Cancelled — nothing was destroyed.")
            return False
        return True

    from teardown.runner import run_terminal_teardown
    try:
        report = run_terminal_teardown(
            comp_dir, teams, boxes, name, env={**os.environ},
            full=full, end_of_competition=args.end_of_competition,
            keep_red=args.keep_red, artifacts_timeout=args.artifacts_timeout,
            confirm=confirm)
    except Refusal as r:
        print(f"\n  REFUSED — {r}")
        sys.exit(1)
    if report is None:
        sys.exit()

    print("\n─── Verification " + "─" * 48)
    print(report.render())
    if not report.clean:
        sys.exit(1)

    print(f"\nInfrastructure for '{competition}' destroyed; estate verified clean.")
    print(f"Competition files preserved at competitions/{competition}/")


if __name__ == "__main__":
    main()
