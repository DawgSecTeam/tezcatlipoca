"""verify-competition.py's command line: argument parsing and the run/exit-code flow."""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from verifier.context import ENV_PATH, build_ctx
from verifier.creds import load_admin_password
from verifier.freeze import do_freeze, do_unfreeze
from verifier.loaders import load_boxes, load_teams
from verifier.model import CheckError
from verifier.runner import PacketProfileError, run_gates
from verifier.summary import print_summary
from verifier.verdict import RunBudget, gate_verdict


def build_parser():
    parser = argparse.ArgumentParser(description="Verify a deployed Quotient competition range.")
    parser.add_argument("comp_dir", help="path to competitions/<id>")
    parser.add_argument("--engine-ip", help="override the scoring-engine IP (skip Terraform)")
    parser.add_argument("--admin-password", help="override the Quotient admin password")
    parser.add_argument("--strict-services", action="store_true",
                        help="also require every service UP for a passing exit code")
    parser.add_argument("--allow-unverified", action="append", default=[],
                        dest="allow_unverified", metavar="GATE",
                        help="waive ONE gate's SKIP (could-not-evaluate) verdict so it "
                             "does not fail the exit code; repeatable, e.g. "
                             "--allow-unverified isolation. Without it a gate that "
                             "couldn't run is NOT a pass — a dead SSH must not exit 0.")
    parser.add_argument("--fix-round-loop", action="store_true", dest="fix_round_loop",
                        help="when the scoring round loop looks stopped after an engine "
                             "reboot, run the start/unpause POSTs instead of only warning")
    parser.add_argument("--expect-no-vulns", action="store_true", dest="expect_no_vulns",
                        help="validation comps that deliberately plant zero misconfigurations "
                             "(box_vulns.json all-empty): skip the misconfig gates instead of "
                             "failing on them")
    parser.add_argument("--packet", dest="packet_profile", default=None,
                        help="packet profile (packets/<event>/packet.yaml): adds packet-"
                             "fidelity gates — credentials.txt must match the packet's "
                             "published default credentials, and the packet's out-of-scope "
                             "accounts must exist on the boxes")
    parser.add_argument("--freeze", action="store_true",
                        help="M4: after a PASSING verify (all gates + plant-coverage), "
                             "record the template hashes + code commit in .frozen.json. "
                             "Windows/domain lineups also need --windows-domain-validated.")
    parser.add_argument("--windows-domain-validated", action="store_true", dest="windows_domain_validated",
                        help="operator attestation that this run exercised the Windows/"
                             "domain validation (DomainSIDs unique per team, machine SIDs "
                             "assessed, three-pass ordering held) — required to freeze "
                             "such lineups.")
    parser.add_argument("--unfreeze", action="store_true",
                        help="M4: remove .frozen.json (needs --confirm-unfreeze; for use "
                             "BEFORE the competition starts).")
    parser.add_argument("--confirm-unfreeze", action="store_true", dest="confirm_unfreeze")
    parser.add_argument("--red-identity", action="store_true", dest="red_identity",
                        help="also verify red's routed identity: red01 must reach a box with "
                             "its red-segment source IP (not the team gateway). Needs red01 "
                             "deployed; seg IP falls back to ../bad-auto/config.yaml")
    parser.add_argument("--red-teams", choices=["own", "all"], default="own",
                        help="--red-teams all: additionally prove red01 can reach EVERY "
                             "team over SSH (one box each), not just its own segment. This "
                             "is the gate the scale8 soak lacked — routed red could not "
                             "reach any satellite team and it went unseen for 40 minutes. "
                             "Needs red01 deployed; pairs with --red-identity.")
    parser.add_argument("--red-ip", default="10.0.0.198", help="red01 mgmt IP for --red-identity")
    parser.add_argument("--red-user", default="sysadmin", help="red01 SSH user for --red-identity")
    parser.add_argument("--red-seg-ip", default=None,
                        help="red01's red-segment address for --red-identity "
                             "(default: ../bad-auto/config.yaml, else 10.200.0.10)")
    parser.add_argument("--timeout", type=int, default=0, metavar="SECONDS",
                        help="overall wall-clock budget for the gate run (default 0 = no "
                             "budget). Checked BETWEEN gates, never mid-flight, so it "
                             "never interrupts a Proxmox task; a gate that never ran is "
                             "SKIP — deliberately non-passing — so a budget can bound the "
                             "run but can never turn an unverified range into a PASS "
                             "(waive with --allow-unverified <gate>).")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    load_dotenv(ENV_PATH)

    # Starts before the terraform/ctx load so the budget covers the whole run; disabled
    # (and therefore inert) at the default --timeout 0.
    budget = RunBudget(args.timeout)

    comp_dir = Path(args.comp_dir).resolve()
    if not comp_dir.is_dir():
        print(f"ERROR: competition directory not found: {comp_dir}", file=sys.stderr)
        return 2

    # Multi-node: activate the recorded placement (env -> engine host, node routes
    # for any node-scoped API call) before anything talks to Proxmox.
    from nodes_ops import activate_placement, read_placement
    placement = read_placement(comp_dir)
    if placement:
        activate_placement(placement)

    try:
        ctx = build_ctx(args, comp_dir)
        teams = load_teams(comp_dir)
        admin_password = load_admin_password(comp_dir, args.admin_password)
        boxes = load_boxes(comp_dir)
    except CheckError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    engine_ip = ctx["scoring_engine_ip"]
    base_url = f"http://{engine_ip}"
    print(f"Verifying competition '{comp_dir.name}' against engine {base_url}")

    if args.unfreeze:
        return 0 if do_unfreeze(comp_dir, args.confirm_unfreeze) else 1

    try:
        outcome = run_gates(args, comp_dir, ctx, teams, admin_password, boxes,
                            base_url, budget)
    except PacketProfileError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    results, coverage_result = outcome.results, outcome.coverage_result

    gate, passed = gate_verdict(results, args.allow_unverified)
    print_summary(results, args.allow_unverified, comp_dir)

    print("\n" + ("RESULT: PASS — competition looks healthy."
                  if passed else "RESULT: FAIL — see failing checks above."))
    if args.freeze:
        # coverage_result is None only when the budget skipped the gate; a gate that
        # never ran cannot attest coverage, so the freeze is refused (fail-closed).
        ok = do_freeze(comp_dir, args, gate,
                       coverage_result is not None and coverage_result.passed)
        return 0 if (passed and ok) else 1
    return 0 if passed else 1


def run():
    """Script entry: main() with the Ctrl-C contract (exit 130)."""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
