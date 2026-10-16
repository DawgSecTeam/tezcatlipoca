"""Gate orchestration: the ordered gate sequence of one verify run.

Every gate reports a GateResult; the SUMMARY and the exit code are both derived from the
list this produces (D6), so a gate's printed word can never disagree with its effect on
the verdict. Gates are called through their modules (`scoreboard.check_services`, ...) so
a test patches the one defining module.
"""

import json
from dataclasses import dataclass, field

from quotient.setup import expected_service_names

from verifier import (domains, engine, firewall, isolation, misconfig, packet, portal,
                      red, remote_access, scoreboard, state_gates)
from verifier.model import CheckError, bool_gate, gate_skip


class PacketProfileError(Exception):
# --packet named a profile that could not be loaded (message is the operator text).
    pass


@dataclass
class RunOutcome:
    results: list = field(default_factory=list)
    coverage_result: object = None


class _Run:

    def __init__(self):
        self.results = []

    def add(self, result):
        self.results.append(result)


def _load_packet_profile(path):
    try:
        from config.packet_ops import load_profile
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
# Run a red01 gate; a CheckError (couldn't run at all) is a SKIP, never a pass.
    try:
        run.add(fn())
    except CheckError as e:
        print(f"  SKIP  — {label} check couldn't run: {e}")
        run.add(gate_skip(name, "check couldn't run"))


def run_gates(args, comp_dir, ctx, teams, admin_password, boxes, base_url):
# Run every gate in order; returns a RunOutcome. Raises PacketProfileError.
    if getattr(args, "interim", False):
        # The deploy-time verify runs BEFORE red/blue staging, and staging is what registers
        # the pins — live 2026-10-15 the same `pins_registered` gate read FAIL here and PASS
        # three minutes later with nothing else changed. Say which reading this is.
        print("\n  INTERIM VERIFY — the deploy-time reading, taken before red/blue staging: a "
              "gate the staging still changes (pins_registered) is not the scoring verdict. "
              "The pre-T0 verify after staging is the authoritative one.")
    run = _Run()
    logins_ok, admin_session = scoreboard.check_logins(base_url, teams, admin_password)
    run.add(bool_gate("logins", logins_ok))
    print("\n  (default-credential regression guard)")
    if args.packet_profile:
        packet_profile = _load_packet_profile(args.packet_profile)
        print("\n  (packet fidelity — credentials + out-of-scope accounts)")
        run.add(bool_gate("packet_creds",
                          packet.check_packet_creds(comp_dir, packet_profile)))
        run.add(packet.check_packet_accounts(ctx, packet_profile, boxes))
    expected_names = _expected_service_names(comp_dir)
    run.results.extend(scoreboard.check_services(
        base_url, admin_session, teams, args.strict_services, expected_names))
    run.add(isolation.check_isolation(ctx, teams, boxes))
    run.add(firewall.check_firewall_in_path(ctx, comp_dir, teams))
    run.add(portal.check_portal_gate(ctx, comp_dir, teams, admin_password))
    try:
        run.add(remote_access.check_remote_access(comp_dir, teams, ctx))
    except CheckError as e:
        print(f"  SKIP  — remote-access check couldn't run: {e}")
        run.add(gate_skip("remote_access", "check couldn't run"))
    if args.red_identity:
        seg_ip = args.red_seg_ip or red.default_red_seg_ip(comp_dir)
        _red_gate(run, "red_identity", "red identity",
                  lambda: red.check_red_identity(ctx, boxes, args.red_ip, seg_ip,
                                                 red_user=args.red_user))
    if args.red_teams == "all":
        _red_gate(run, "red_teams", "red team-reachability",
                  lambda: red.check_red_teams(ctx, boxes, args.red_ip,
                                              red_user=args.red_user))
    clean = args.expect_no_vulns or misconfig.comp_is_clean(comp_dir, boxes)
    if clean:
        why = ("--expect-no-vulns" if args.expect_no_vulns
               else "box_vulns.json is empty and no machine carries a misconfig")
        print("\n[4/5] MISCONFIG SPOT-CHECK")
        print(f"  SKIP  — {why}: this comp deliberately plants no misconfigurations")
        note = f"{why}: comp plants no misconfigurations"
        run.add(gate_skip("misconfig", note, gating=False))
    elif misconfig.misconfigs_unverifiable(comp_dir, boxes):
        note = ("pinned misconfigs have no verify probe (e.g. Windows-only); plant_coverage "
                "proves they were planted")
        print("\n[4/5] MISCONFIG SPOT-CHECK")
        print(f"  SKIP  — {note}")
        run.add(gate_skip("misconfig", note, gating=False))
    else:
        run.add(misconfig.check_misconfig(ctx, boxes, comp_dir))
    injects_relevant, injects_ok = engine.check_injects(base_url, admin_session, comp_dir)
    if injects_relevant:
        run.add(bool_gate("injects", injects_ok))
    else:
        run.add(gate_skip("injects", "competition ships no injects/ dir", gating=False))
    run.add(engine.check_round_loop(base_url, admin_session, fix=args.fix_round_loop))
    print("\n  (M4 plant coverage — expected vs. actually planted, per machine)")
    coverage_result = state_gates.check_plant_coverage(comp_dir)
    run.add(coverage_result)
    print("\n  (tolerated failures — the deploy's degradation ledger)")
    run.add(state_gates.check_degradations(comp_dir))
    print("\n  (AD domains — promotion, joins, AD plants, DomainSID uniqueness)")
    run.add(domains.check_domains(comp_dir, teams, boxes, ctx=ctx))
    return RunOutcome(run.results, coverage_result)
