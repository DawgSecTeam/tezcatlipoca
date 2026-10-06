"""Gate orchestration: the ordered gate sequence of one verify run, honoring --timeout.

Every gate reports a GateResult; the SUMMARY and the exit code are both derived from the
list this produces (D6), so a gate's printed word can never disagree with its effect on
the verdict. Gates are called through their modules (`scoreboard.check_services`, ...) so
a test patches the one defining module.
"""

import json
from dataclasses import dataclass, field

from quotient.setup import expected_service_names

from verifier import (creds, domains, engine, firewall, isolation, misconfig, packet, red, reports,
                      scoreboard, state_gates)
from verifier.model import CheckError, bool_gate, gate_skip


class PacketProfileError(Exception):
    """--packet named a profile that could not be loaded (message is the operator text)."""


@dataclass
class RunOutcome:
    results: list = field(default_factory=list)
    coverage_result: object = None  # GateResult; None only when the budget skipped the gate


class _Run:
    """Accumulates results and applies the --timeout guard between gates."""

    def __init__(self, budget):
        self.budget = budget
        self.results = []

    def spent(self, name, label=""):
        """--timeout guard, checked BETWEEN gates (never mid-flight): a gate that never
        ran is SKIP_UNAVAILABLE — deliberately non-passing — so a budget can bound the
        run but can never turn an unevaluated range into a PASS. Inert at --timeout 0."""
        if not self.budget.expired():
            return False
        print(f"  SKIP  — run budget ({self.budget.label}) exhausted: '{name}' not evaluated")
        self.results.append(gate_skip(name, "run budget exhausted before the gate ran",
                                      label=label))
        return True

    def add(self, result):
        self.results.append(result)


def _load_packet_profile(path):
    try:
        from packet_ops import load_profile
        return load_profile(path)
    except SystemExit as e:
        raise PacketProfileError(str(e))


def _expected_service_names(comp_dir):
    # boxes.json (box TYPES, keyed by name in box_services.json) — nakon-config
    # machines carry team-suffixed names the pin map doesn't use
    try:
        box_list = json.loads((comp_dir / "boxes.json").read_text())
        pinned_services = json.loads((comp_dir / "box_services.json").read_text())
    except (OSError, ValueError):
        box_list, pinned_services = [], {}
    return expected_service_names(pinned_services, box_list) if pinned_services else set()


def _red_gate(run, name, label, fn):
    """Run a red01 gate; a CheckError (couldn't run at all) is a SKIP, never a pass."""
    try:
        run.add(fn())
    except CheckError as e:
        print(f"  SKIP  — {label} check couldn't run: {e}")
        run.add(gate_skip(name, "check couldn't run"))


def run_gates(args, comp_dir, ctx, teams, admin_password, boxes, base_url, budget):
    """Run every gate in order; returns a RunOutcome. Raises PacketProfileError."""
    run = _Run(budget)
    # Only left None when logins itself was budget-skipped; every consumer below is
    # guarded by the same monotonically expiring budget, so it cannot be reached.
    admin_session = None

    if not run.spent("logins"):
        logins_ok, admin_session = scoreboard.check_logins(base_url, teams, admin_password)
        run.add(bool_gate("logins", logins_ok))
    print("\n  (default-credential regression guard)")
    if not run.spent("no_default_creds"):
        run.add(bool_gate("no_default_creds", creds.check_no_default_creds(comp_dir)))
    if args.packet_profile:
        packet_profile = _load_packet_profile(args.packet_profile)
        print("\n  (packet fidelity — credentials + out-of-scope accounts)")
        if not run.spent("packet_creds"):
            run.add(bool_gate("packet_creds",
                              packet.check_packet_creds(comp_dir, packet_profile)))
            run.add(packet.check_packet_accounts(ctx, packet_profile, boxes))
    expected_names = _expected_service_names(comp_dir)
    if not run.spent("services"):
        run.results.extend(scoreboard.check_services(
            base_url, admin_session, teams, args.strict_services, expected_names))
    if not run.spent("isolation"):
        run.add(isolation.check_isolation(ctx, teams, boxes))
    if not run.spent("firewall_in_path"):
        run.add(firewall.check_firewall_in_path(ctx, comp_dir, teams))
    if args.red_identity:
        seg_ip = args.red_seg_ip or red.default_red_seg_ip()
        if not run.spent("red_identity"):
            _red_gate(run, "red_identity", "red identity",
                      lambda: red.check_red_identity(ctx, boxes, args.red_ip, seg_ip,
                                                     red_user=args.red_user))
    if args.red_teams == "all" and not run.spent("red_teams"):
        _red_gate(run, "red_teams", "red team-reachability",
                  lambda: red.check_red_teams(ctx, boxes, args.red_ip,
                                              red_user=args.red_user))
    print("\n  (live-ops health check status — informational)")
    if not budget.expired():
        reports.report_healthcheck_status(ctx)
    clean = args.expect_no_vulns or misconfig.comp_is_clean(comp_dir, boxes)
    if clean:
        why = ("--expect-no-vulns" if args.expect_no_vulns
               else "box_vulns.json is empty and no machine carries a misconfig")
        print("\n[4/5] MISCONFIG SPOT-CHECK")
        print(f"  SKIP  — {why}: this comp deliberately plants no misconfigurations")
        note = f"{why}: comp plants no misconfigurations"
        run.add(gate_skip("misconfig", note, gating=False))
        run.add(gate_skip("misconfig_survival", note, gating=False))
    elif misconfig.misconfigs_unverifiable(comp_dir, boxes):
        note = ("pinned misconfigs have no verify probe (e.g. Windows-only); plant_coverage "
                "proves they were planted")
        print("\n[4/5] MISCONFIG SPOT-CHECK")
        print(f"  SKIP  — {note}")
        run.add(gate_skip("misconfig", note, gating=False))
        run.add(gate_skip("misconfig_survival", note, gating=False))
    elif not run.spent("misconfig"):
        run.add(bool_gate("misconfig", misconfig.check_misconfig(ctx, boxes, comp_dir)))
        run.add(misconfig.check_misconfig_survival(ctx, boxes))
    if not budget.expired():
        reports.report_beacons(ctx, boxes)
    if not run.spent("injects"):
        injects_relevant, injects_ok = engine.check_injects(base_url, admin_session, comp_dir)
        if injects_relevant:
            run.add(bool_gate("injects", injects_ok))
        else:
            run.add(gate_skip("injects", "competition ships no injects/ dir", gating=False))
    if not run.spent("round_loop"):
        run.add(engine.check_round_loop(base_url, admin_session, fix=args.fix_round_loop))
    print("\n  (M4 plant coverage — expected vs. actually planted, per machine)")
    coverage_result = None
    if not run.spent("plant_coverage"):
        coverage_result = state_gates.check_plant_coverage(comp_dir)
        run.add(coverage_result)
    print("\n  (Tolerated failures — prerequisites that failed while the deploy continued)")
    run.add(state_gates.check_degradations(comp_dir))
    print("\n  (AD domains — promotion, joins, AD plants, DomainSID uniqueness)")
    if not run.spent("domains"):
        run.add(domains.check_domains(comp_dir, teams, boxes, ctx=ctx))
    return RunOutcome(run.results, coverage_result)
