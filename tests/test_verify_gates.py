"""verify-competition.py gate correctness: fail-closed plant coverage, tri-state
SKIP semantics, isolation decision table, strict-services freshness, and the
SUMMARY/exit-code single source of truth.

Each test corresponds to a reproduced defect:
  D1 check_services 2-tuple vs main's 3-tuple unpack (half-deployed engine crash)
  D2 plant_coverage fails OPEN when .deploy_state.json has no coverage record
  D3 packet_accounts / misconfig_survival turn a SKIP into a PASS
  D4 misconfig_survival had no branch for "absent on every team"
  D5 isolation called a dead target "blocked as expected"
  D6 tri-state GateResult + SUMMARY generated from it (+ --allow-unverified)
  D7 --strict-services vacuous with nothing scored / no freshness precondition
  D8 stale coverage entries failed the gate; f-string/`missing` pyflakes items
"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
_SPEC = importlib.util.spec_from_file_location(
    "verify_gates_test", _REPO / "verify-competition.py")
verify = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify)


# --------------------------------------------------------------------------- helpers

def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _run(fn, *args, **kwargs):
    """Run a check, returning (result, captured stdout)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = fn(*args, **kwargs)
    return result, out.getvalue()


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Session:
    """Fake requests session: /api/teams + per-team /api/services/<id>."""

    def __init__(self, teams, services, teams_error=None):
        self._teams, self._services, self._teams_error = teams, services, teams_error

    def get(self, url, timeout=None):
        if url.endswith("/api/teams"):
            if self._teams_error is not None:
                raise self._teams_error
            return _Resp(self._teams)
        return _Resp(self._services.get(url.rsplit("/", 1)[-1], []))


def _round(start, passed=True):
    return {"StartTime": start, "Checks": [{"Result": passed}]}


# --------------------------------------------------------------------------- D1

class CheckServicesArity(unittest.TestCase):
    """D1: a failing /api/teams must not crash main's unpacking."""

    def test_api_teams_failure_returns_gate_results(self):
        session = _Session([], {}, teams_error=verify.requests.RequestException("refused"))
        results, out = _run(verify.check_services, "http://e", session, {}, False)
        self.assertTrue(all(isinstance(r, verify.GateResult) for r in results))
        self.assertEqual([r.name for r in results], ["services"])
        self.assertIs(results[0].status, verify.Status.FAIL)
        self.assertFalse(results[0].gating)  # non-strict services is informational
        self.assertIn("could not fetch /api/teams", out)

    def test_api_teams_failure_strict_gates_and_pins_fail(self):
        session = _Session([], {}, teams_error=verify.requests.RequestException("refused"))
        results, _ = _run(verify.check_services, "http://e", session, {}, True,
                          {"web01-ssh"})
        names = [r.name for r in results]
        self.assertEqual(names, ["services(strict)", "pins_registered"])
        self.assertIs(results[0].status, verify.Status.FAIL)
        self.assertTrue(results[0].gating)
        self.assertIs(results[1].status, verify.Status.FAIL)
        gate, passed = verify.gate_verdict(results)
        self.assertFalse(passed)
        self.assertEqual(gate, {"services(strict)": False, "pins_registered": False})

    def test_no_admin_session_is_skip_not_crash(self):
        results, out = _run(verify.check_services, "http://e", None, {}, True,
                            {"web01-ssh"})
        self.assertEqual([r.name for r in results], ["services(strict)", "pins_registered"])
        self.assertIs(results[0].status, verify.Status.SKIP_UNAVAILABLE)
        self.assertIs(results[1].status, verify.Status.SKIP_UNAVAILABLE)
        _, passed = verify.gate_verdict(results)
        self.assertFalse(passed)


# --------------------------------------------------------------------------- D7

class StrictServices(unittest.TestCase):
    """D7: --strict-services must not pass an unscored or frozen scoreboard."""

    def _session(self, services):
        return _Session([{"ID": 1, "Name": "team1"}], {"1": services})

    def test_all_unscored_fails_strict(self):
        services = [{"ServiceName": "web01-http", "Last10Rounds": []},
                    {"ServiceName": "web01-ssh", "Last10Rounds": []}]
        results, out = _run(verify.check_services, "http://e", self._session(services),
                            {}, True)
        self.assertIs(results[0].status, verify.Status.FAIL)
        self.assertIn("never have run a round", out)

    def test_all_unscored_is_informational_without_strict(self):
        services = [{"ServiceName": "web01-http", "Last10Rounds": []}]
        results, _ = _run(verify.check_services, "http://e", self._session(services),
                          {}, False)
        self.assertIs(results[0].status, verify.Status.PASS)
        self.assertFalse(results[0].gating)

    def test_stale_up_fails_strict(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        services = [{"ServiceName": "web01-http", "Last10Rounds": [_round(old)]}]
        results, out = _run(verify.check_services, "http://e", self._session(services),
                            {}, True)
        self.assertIs(results[0].status, verify.Status.FAIL)
        self.assertIn("stale/frozen", out)

    def test_fresh_up_passes_strict(self):
        now = datetime.now(timezone.utc).isoformat()
        services = [{"ServiceName": "web01-http", "Last10Rounds": [_round(now)]}]
        results, out = _run(verify.check_services, "http://e", self._session(services),
                            {}, True)
        self.assertIs(results[0].status, verify.Status.PASS)
        self.assertNotIn("stale", out)

    def test_missing_round_timestamp_fails_strict(self):
        services = [{"ServiceName": "web01-http",
                     "Last10Rounds": [{"Checks": [{"Result": True}]}]}]
        results, out = _run(verify.check_services, "http://e", self._session(services),
                            {}, True)
        self.assertIs(results[0].status, verify.Status.FAIL)
        self.assertIn("no parseable round StartTime", out)

    def test_pins_registered_gates_missing_pin(self):
        now = datetime.now(timezone.utc).isoformat()
        services = [{"ServiceName": "web01-http", "Last10Rounds": [_round(now)]}]
        results, out = _run(verify.check_services, "http://e", self._session(services),
                            {}, False, {"web01-http", "web01-ssh"})
        pins = next(r for r in results if r.name == "pins_registered")
        self.assertIs(pins.status, verify.Status.FAIL)
        self.assertIn("never registered", out)

    def test_pins_registered_passes(self):
        now = datetime.now(timezone.utc).isoformat()
        services = [{"ServiceName": "web01-http", "Last10Rounds": [_round(now)]}]
        results, _ = _run(verify.check_services, "http://e", self._session(services),
                          {}, False, {"web01-http"})
        pins = next(r for r in results if r.name == "pins_registered")
        self.assertIs(pins.status, verify.Status.PASS)


class RoundLoop(unittest.TestCase):
    """D7: check_round_loop's tri-state is real and consumed by the gate verdict."""

    def _session(self, payload):
        return _Session(payload, {})

    def test_stopped_loop_is_fail_and_non_passing(self):
        payload = {"running": True, "current_round_time": "0001-01-01T00:00:00Z",
                   "last_round": {"StartTime": "2020-01-01T00:00:00Z"}}
        session = self._session(payload)
        session.get = lambda url, timeout=None: _Resp(payload)
        result, out = _run(verify.check_round_loop, "http://e", session)
        self.assertIs(result.status, verify.Status.FAIL)
        self.assertIn("WARN", out)  # diagnostic kept from the old informational message
        _, passed = verify.gate_verdict([result])
        self.assertFalse(passed)

    def test_paused_engine_passes(self):
        payload = {"running": False, "current_round_time": "0001-01-01T00:00:00Z"}
        session = self._session(payload)
        session.get = lambda url, timeout=None: _Resp(payload)
        result, _ = _run(verify.check_round_loop, "http://e", session)
        self.assertIs(result.status, verify.Status.PASS)

    def test_unreadable_engine_is_skip(self):
        result, _ = _run(verify.check_round_loop, "http://e", None)
        self.assertIs(result.status, verify.Status.SKIP_UNAVAILABLE)


# --------------------------------------------------------------------------- D2/D8

MACHINES = [
    {"name": "web01-team104", "ip": "192.168.104.5",
     "configurations": ["suid-find", "www-data-shell"]},
]


def _comp_dir(machines=None):
    tmp = tempfile.TemporaryDirectory()
    comp_dir = Path(tmp.name)
    (comp_dir / "nakon-config.json").write_text(
        json.dumps({"machines": machines if machines is not None else MACHINES}))
    return tmp, comp_dir


def _state(comp_dir, payload):
    (comp_dir / ".deploy_state.json").write_text(json.dumps(payload))


class PlantCoverage(unittest.TestCase):
    """D2: the gate must fail CLOSED when coverage was never recorded."""

    def test_missing_state_file_fails(self):
        tmp, comp_dir = _comp_dir()
        self.addCleanup(tmp.cleanup)
        result, out = _run(verify.check_plant_coverage, comp_dir)
        self.assertIs(result.status, verify.Status.FAIL)
        self.assertIn("coverage was never recorded", out)
        _, passed = verify.gate_verdict([result])
        self.assertFalse(passed)

    def test_missing_coverage_key_fails(self):
        tmp, comp_dir = _comp_dir()
        self.addCleanup(tmp.cleanup)
        _state(comp_dir, {"phase": 7})
        result, out = _run(verify.check_plant_coverage, comp_dir)
        self.assertIs(result.status, verify.Status.FAIL)
        self.assertIn("never recorded", out)

    def test_unparseable_state_fails(self):
        tmp, comp_dir = _comp_dir()
        self.addCleanup(tmp.cleanup)
        (comp_dir / ".deploy_state.json").write_text("{not json")
        result, _ = _run(verify.check_plant_coverage, comp_dir)
        self.assertIs(result.status, verify.Status.FAIL)

    def test_empty_coverage_with_failed_tally_fails(self):
        """The exact shape that used to PASS while SUMMARY printed WARNING."""
        tmp, comp_dir = _comp_dir()
        self.addCleanup(tmp.cleanup)
        _state(comp_dir, {"plant_coverage_failed": {},
                          "nakon_failed_steps": ["final: web01-team104 suid-find rc=1"]})
        result, out = _run(verify.check_plant_coverage, comp_dir)
        self.assertIs(result.status, verify.Status.FAIL)
        self.assertIn("FAILED plant step", out)

    def test_genuine_coverage_failures_fail(self):
        tmp, comp_dir = _comp_dir()
        self.addCleanup(tmp.cleanup)
        _state(comp_dir, {"plant_coverage_failed": {"web01-team104": ["suid-find"]},
                          "nakon_failed_steps": []})
        result, out = _run(verify.check_plant_coverage, comp_dir)
        self.assertIs(result.status, verify.Status.FAIL)
        self.assertIn("web01-team104: not planted: suid-find", out)

    def test_clean_coverage_and_tally_passes(self):
        tmp, comp_dir = _comp_dir()
        self.addCleanup(tmp.cleanup)
        _state(comp_dir, {"plant_coverage_failed": {}, "nakon_failed_steps": []})
        result, out = _run(verify.check_plant_coverage, comp_dir)
        self.assertIs(result.status, verify.Status.PASS)
        self.assertIn("full config coverage", out)

    def test_missing_record_but_clean_tally_passes_via_fallback(self):
        """deploy.py:105 promises this fallback: no --json outcome (older nakon)."""
        tmp, comp_dir = _comp_dir()
        self.addCleanup(tmp.cleanup)
        _state(comp_dir, {"nakon_failed_steps": []})
        result, out = _run(verify.check_plant_coverage, comp_dir)
        self.assertIs(result.status, verify.Status.PASS)
        self.assertIn("falls back", out)

    def test_stale_failure_entry_is_filtered(self):
        """D8: a failure for a config no longer in `configurations` must not fail."""
        tmp, comp_dir = _comp_dir()
        self.addCleanup(tmp.cleanup)
        _state(comp_dir, {"plant_coverage_failed": {"web01-team104": ["legacy-config"]},
                          "nakon_failed_steps": []})
        result, _ = _run(verify.check_plant_coverage, comp_dir)
        self.assertIs(result.status, verify.Status.PASS)

    def test_golden_stage_failure_maps_onto_team_clone(self):
        tmp, comp_dir = _comp_dir()
        self.addCleanup(tmp.cleanup)
        _state(comp_dir, {"plant_coverage_failed": {"web01-golden": ["suid-find"]},
                          "nakon_failed_steps": []})
        result, out = _run(verify.check_plant_coverage, comp_dir)
        self.assertIs(result.status, verify.Status.FAIL)
        self.assertIn("suid-find (golden-stage)", out)

    def test_stale_golden_entry_is_filtered(self):
        tmp, comp_dir = _comp_dir()
        self.addCleanup(tmp.cleanup)
        _state(comp_dir, {"plant_coverage_failed": {"web01-golden": ["legacy-config"]},
                          "nakon_failed_steps": []})
        result, _ = _run(verify.check_plant_coverage, comp_dir)
        self.assertIs(result.status, verify.Status.PASS)

    def test_golden_slot_failure_maps_onto_team_clone(self):
        """Satellite slot N records '{box}-golden-slot{N}' (every slot's stage config
        names its golden machine identically); a failure on any slot's golden flags
        every team copy of the box."""
        tmp, comp_dir = _comp_dir()
        self.addCleanup(tmp.cleanup)
        _state(comp_dir, {"plant_coverage_failed": {"web01-golden-slot2": ["suid-find"]},
                          "nakon_failed_steps": []})
        result, out = _run(verify.check_plant_coverage, comp_dir)
        self.assertIs(result.status, verify.Status.FAIL)
        self.assertIn("suid-find (golden-stage)", out)

    def test_stale_golden_slot_entry_is_filtered(self):
        tmp, comp_dir = _comp_dir()
        self.addCleanup(tmp.cleanup)
        _state(comp_dir, {"plant_coverage_failed": {"web01-golden-slot2": ["legacy-config"]},
                          "nakon_failed_steps": []})
        result, _ = _run(verify.check_plant_coverage, comp_dir)
        self.assertIs(result.status, verify.Status.PASS)

    def test_another_boxs_golden_keys_do_not_map(self):
        """Entries keyed for other boxes (a different box's golden, or another team's
        machine key) must not fail this machine."""
        tmp, comp_dir = _comp_dir()
        self.addCleanup(tmp.cleanup)
        _state(comp_dir, {"plant_coverage_failed": {"web99-golden": ["suid-find"],
                                                    "web01-team999": ["suid-find"]},
                          "nakon_failed_steps": []})
        result, out = _run(verify.check_plant_coverage, comp_dir)
        self.assertIs(result.status, verify.Status.PASS)
        self.assertNotIn("not planted", out)

    def test_no_nakon_config_is_a_non_gating_skip(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result, _ = _run(verify.check_plant_coverage, Path(tmp.name))
        self.assertIs(result.status, verify.Status.SKIP_UNAVAILABLE)
        self.assertFalse(result.gating)


# --------------------------------------------------------------------------- D3

PACKET_PROFILE = {"credentials": {"out_of_scope": ["scorebot", "blackteam"]}}
LINUX_BOXES = [{"name": "web01", "ip": "192.168.104.5", "os": "ubuntu"}]
CTX = {"ssh_key_path": "/k", "scoring_engine_ip": "10.0.0.1", "vm_username": "u"}


class PacketAccounts(unittest.TestCase):
    """D3: unprovable out-of-scope accounts are a SKIP, never a pass."""

    def test_ssh_dead_is_skip(self):
        with patch.object(verify, "ssh_via_gateway",
                          side_effect=verify.CheckError("ssh: connect refused")):
            result, out = _run(verify.check_packet_accounts, CTX, PACKET_PROFILE, LINUX_BOXES)
        self.assertIs(result.status, verify.Status.SKIP_UNAVAILABLE)
        self.assertTrue(result.gating)
        self.assertIn("SKIP", out)
        _, passed = verify.gate_verdict([result])
        self.assertFalse(passed)

    def test_probe_rc_nonzero_is_skip(self):
        with patch.object(verify, "ssh_via_gateway",
                          return_value=_proc(255, "", "Permission denied")):
            result, out = _run(verify.check_packet_accounts, CTX, PACKET_PROFILE, LINUX_BOXES)
        self.assertIs(result.status, verify.Status.SKIP_UNAVAILABLE)
        self.assertIn("SKIP", out)

    def test_all_accounts_present_passes(self):
        with patch.object(verify, "ssh_via_gateway",
                          return_value=_proc(0, "scorebot=1\nblackteam=1\n")):
            result, _ = _run(verify.check_packet_accounts, CTX, PACKET_PROFILE, LINUX_BOXES)
        self.assertIs(result.status, verify.Status.PASS)

    def test_missing_account_fails(self):
        with patch.object(verify, "ssh_via_gateway",
                          return_value=_proc(0, "scorebot=1\nblackteam=0\n")):
            result, out = _run(verify.check_packet_accounts, CTX, PACKET_PROFILE, LINUX_BOXES)
        self.assertIs(result.status, verify.Status.FAIL)
        self.assertIn("blackteam", out)

    def test_no_linux_box_is_skip(self):
        windows_only = [{"name": "dc01", "ip": "192.168.104.6", "os": "windows"}]
        result, _ = _run(verify.check_packet_accounts, CTX, PACKET_PROFILE, windows_only)
        self.assertIs(result.status, verify.Status.SKIP_UNAVAILABLE)


# --------------------------------------------------------------------------- D3/D4

SURVIVAL_BOXES = [
    {"name": "web01-team104", "ip": "192.168.104.5", "configurations": ["suid-find"]},
    {"name": "web01-team105", "ip": "192.168.105.5", "configurations": ["suid-find"]},
]
PRESENT_OUT = "-rwsr-xr-x 1 root root 12345 /usr/bin/find\n"
ABSENT_OUT = "-rwxr-xr-x 1 root root 12345 /usr/bin/find\n"


class MisconfigSurvival(unittest.TestCase):
    """D3 (all-unknown SKIP) and D4 (absent-everywhere had no branch)."""

    def _run_with_outputs(self, outputs_by_ip):
        def fake(ctx, ip, cmd, timeout=60):
            value = outputs_by_ip.get(ip)
            if value is None:
                raise verify.CheckError("ssh: connect refused")
            return _proc(0, value)
        with patch.object(verify, "ssh_via_gateway", side_effect=fake):
            return _run(verify.check_misconfig_survival, CTX, SURVIVAL_BOXES)

    def test_present_on_every_team_passes(self):
        result, out = self._run_with_outputs({
            "192.168.104.5": PRESENT_OUT, "192.168.105.5": PRESENT_OUT})
        self.assertIs(result.status, verify.Status.PASS)
        self.assertIn("present on all 2 team(s)", out)

    def test_present_on_one_team_fails(self):
        result, _ = self._run_with_outputs({
            "192.168.104.5": PRESENT_OUT, "192.168.105.5": ABSENT_OUT})
        self.assertIs(result.status, verify.Status.FAIL)

    def test_absent_everywhere_fails(self):
        """D4: this matched no branch at all before — no output, all_ok stayed True."""
        result, out = self._run_with_outputs({
            "192.168.104.5": ABSENT_OUT, "192.168.105.5": ABSENT_OUT})
        self.assertIs(result.status, verify.Status.FAIL)
        self.assertIn("absent on every team", out)

    def test_all_unknown_is_skip(self):
        """D3: a dead SSH on every clone must not read as a PASS."""
        result, out = self._run_with_outputs({})
        self.assertIs(result.status, verify.Status.SKIP_UNAVAILABLE)
        self.assertTrue(result.gating)
        self.assertIn("SKIP", out)
        _, passed = verify.gate_verdict([result])
        self.assertFalse(passed)


# --------------------------------------------------------------------------- D5

FORWARD_OK = "-A FORWARD -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP\n"
TEAMS = {"team1": {"identifier": 104}, "team2": {"identifier": 105}}
ISO_BOXES = [
    {"name": "web01-team104", "ip": "192.168.104.5", "os": "ubuntu"},
    {"name": "web01-team105", "ip": "192.168.105.5", "os": "ubuntu"},
]


class IsolationDecisionTable(unittest.TestCase):
    """D5: "blocked" must be reachable-but-for-the-rule, not just any RC!=0."""

    def _run(self, *, rule=FORWARD_OK, forward_rc=0, engine_exc=None,
             cross="blocked", cross_exc=None, internet="ok", liveness="live",
             teams=TEAMS, boxes=ISO_BOXES):
        def fake_engine(ctx, cmd, timeout=30):
            if engine_exc:
                raise verify.CheckError(engine_exc)
            if "iptables" in cmd:
                return _proc(forward_rc, rule, "")
            if liveness == "live":
                return _proc(0, "RC=0\n", "")
            if liveness == "dead":
                return _proc(0, "RC=1\n", "")
            raise verify.CheckError("engine ssh broke")

        def fake_gateway(ctx, ip, cmd, timeout=60):
            if "/dev/tcp/1.1.1.1/443" in cmd:
                if internet == "ok":
                    return _proc(0, "RC=0\n", "")
                if internet == "fail":
                    return _proc(0, "RC=1\n", "")
                raise verify.CheckError("control ssh broke")
            if cross_exc:
                raise verify.CheckError(cross_exc)
            return _proc(0, "RC=0\n" if cross == "reachable" else "RC=1\n", "")

        with patch.object(verify, "ssh_to_engine", side_effect=fake_engine), \
             patch.object(verify, "ssh_via_gateway", side_effect=fake_gateway):
            return _run(verify.check_isolation, CTX, teams, boxes)

    def test_rule_absent_fails(self):
        result, out = self._run(rule="-A FORWARD -j ACCEPT\n")
        self.assertIs(result.status, verify.Status.FAIL)
        self.assertIn("no DROP rule covering", out)

    def test_rule_present_target_reachable_fails(self):
        result, out = self._run(cross="reachable")
        self.assertIs(result.status, verify.Status.FAIL)
        self.assertIn("CAN reach", out)

    def test_target_closed_is_skip(self):
        result, out = self._run(liveness="dead")
        self.assertIs(result.status, verify.Status.SKIP_UNAVAILABLE)
        self.assertIn("dead box and a blocked one look identical", out)
        _, passed = verify.gate_verdict([result])
        self.assertFalse(passed)

    def test_verifier_dead_is_skip(self):
        result, _ = self._run(cross_exc="ssh: connect refused")
        self.assertIs(result.status, verify.Status.SKIP_UNAVAILABLE)

    def test_verifier_rc_nonzero_is_skip(self):
        with patch.object(verify, "ssh_to_engine", return_value=_proc(0, FORWARD_OK, "")), \
             patch.object(verify, "ssh_via_gateway", return_value=_proc(255, "", "denied")):
            result, _ = _run(verify.check_isolation, CTX, TEAMS, ISO_BOXES)
        self.assertIs(result.status, verify.Status.SKIP_UNAVAILABLE)

    def test_engine_dead_is_fail(self):
        result, _ = self._run(engine_exc="engine unreachable")
        self.assertIs(result.status, verify.Status.FAIL)

    def test_forward_read_failure_is_fail(self):
        result, _ = self._run(forward_rc=1)
        self.assertIs(result.status, verify.Status.FAIL)

    def test_control_failure_is_skip(self):
        result, out = self._run(internet="fail")
        self.assertIs(result.status, verify.Status.SKIP_UNAVAILABLE)
        self.assertIn("over-blocking", out)

    def test_control_could_not_run_is_skip(self):
        result, _ = self._run(internet="dead")
        self.assertIs(result.status, verify.Status.SKIP_UNAVAILABLE)

    def test_blocked_with_live_target_and_control_passes(self):
        result, out = self._run()
        self.assertIs(result.status, verify.Status.PASS)
        self.assertIn("blocked as expected", out)

    def test_team_without_identifier_does_not_crash(self):
        """The old `t["identifier"]` KeyError took the whole verifier down."""
        result, _ = self._run(teams={"team1": {}, "team2": {"identifier": 105}})
        self.assertIs(result.status, verify.Status.PASS)

    def test_single_team_is_pass_on_rule_presence(self):
        result, _ = self._run(teams={"team1": {"identifier": 104}})
        self.assertIs(result.status, verify.Status.PASS)

    def test_rule_must_cover_derived_subnets(self):
        """A per-subnet rule doesn't isolate every pair; the old literal check would
        also have missed a renumbered range entirely."""
        result, _ = self._run(
            rule="-A FORWARD -s 192.168.104.0/24 -d 192.168.105.0/24 -j DROP\n")
        self.assertIs(result.status, verify.Status.FAIL)

    def test_foreign_supernet_rule_fails(self):
        result, _ = self._run(rule="-A FORWARD -s 10.0.0.0/8 -d 10.0.0.0/8 -j DROP\n")
        self.assertIs(result.status, verify.Status.FAIL)


# --------------------------------------------------------------------------- D6

class TriStateVerdict(unittest.TestCase):
    """D6: GateResult is the single source of truth for SUMMARY + exit code."""

    def test_status_values(self):
        self.assertEqual(verify.Status.PASS.value, "PASS")
        self.assertEqual(verify.Status.FAIL.value, "FAIL")
        self.assertEqual(verify.Status.SKIP_UNAVAILABLE.value, "SKIP")

    def test_result_passed_property(self):
        self.assertTrue(verify._pass("g").passed)
        self.assertFalse(verify._fail("g").passed)
        self.assertFalse(verify._skip("g").passed)

    def test_skip_is_not_passing(self):
        results = [verify._pass("a"), verify._skip("b")]
        gate, passed = verify.gate_verdict(results)
        self.assertFalse(passed)
        self.assertEqual(gate, {"a": True, "b": False})

    def test_allow_unverified_waives_a_named_skip(self):
        results = [verify._pass("a"), verify._skip("b")]
        gate, passed = verify.gate_verdict(results, ["b"])
        self.assertTrue(passed)
        self.assertEqual(gate["b"], False)  # freeze still refuses on a waived gate

    def test_allow_unverified_matches_the_summary_label(self):
        results = [verify._skip("services(strict)", label="services")]
        self.assertFalse(verify.gate_verdict(results, ["nope"])[1])
        self.assertTrue(verify.gate_verdict(results, ["services"])[1])

    def test_fail_is_never_waivable(self):
        results = [verify._fail("isolation")]
        self.assertFalse(verify.gate_verdict(results, ["isolation"])[1])

    def test_non_gating_result_is_excluded_from_the_verdict(self):
        results = [verify._pass("a"), verify._fail("b", gating=False)]
        gate, passed = verify.gate_verdict(results)
        self.assertTrue(passed)
        self.assertNotIn("b", gate)

    def test_summary_word_matches_the_exit_effect(self):
        """The old SUMMARY printed "SKIP — unverified" while the dict held FAIL;
        now the printed word IS the verdict."""
        results = [verify._pass("logins"), verify._skip("isolation")]
        lines = verify.summary_lines(results)
        self.assertIn("SKIP", lines[1])
        _, passed = verify.gate_verdict(results)
        self.assertFalse(passed)
        allowed_lines = verify.summary_lines(results, ["isolation"])
        self.assertIn("--allow-unverified", allowed_lines[1])
        self.assertTrue(verify.gate_verdict(results, ["isolation"])[1])

    def test_informational_line_is_annotated(self):
        lines = verify.summary_lines([verify._fail("services", "some DOWN", gating=False)])
        self.assertIn("[informational]", lines[0])
        self.assertTrue(verify.gate_verdict(
            [verify._fail("services", gating=False)])[1])

    def test_summary_shows_detail(self):
        lines = verify.summary_lines([verify._pass("domains", "2 team domain(s)")])
        self.assertEqual(lines[0].split(":", 1)[1].strip(), "PASS  2 team domain(s)")


class RedTeamsReachability(unittest.TestCase):
    """--red-teams all: red must reach EVERY team, not just one box.

    scale8-soak-2026-10-02: routed red01 could not reach any satellite team box
    (15/15 cred_sprays "unreachable over SSH", 0 footholds) and half the range was
    never attacked. `--red-identity` proved red's source address on a single box, so
    a whole-node routing failure still reported PASS. This gate is the missing one,
    and it is what must fail BEFORE T0 rather than at T+40."""

    CTX = {"ssh_key_path": "/tmp/key"}

    def _boxes(self, *teams):
        """One box per (identifier, last-octet) pair, like nakon-config machines."""
        return [{"name": f"box-{ident}-{last}", "ip": f"192.168.{ident}.{last}"}
                for ident, last in teams]

    def _run(self, boxes, reachable, calls=None):
        """`reachable` = set of IPs red01 can dial; every other probe returns rc=1."""
        def fake_run(cmd, **kw):
            target = cmd[-1].split("/dev/tcp/")[1].split("/")[0]
            if calls is not None:
                calls.append(target)
            ok = target in reachable
            return _proc(returncode=0 if ok else 1, stdout="RED-OK\n" if ok else "")
        with patch.object(verify.subprocess, "run", side_effect=fake_run):
            return verify.check_red_teams(self.CTX, boxes, "10.0.0.196")

    def test_every_team_reachable_passes(self):
        boxes = self._boxes((221, 2), (225, 2))
        res = self._run(boxes, {"192.168.221.2", "192.168.225.2"})
        self.assertIs(res.status, verify.Status.PASS)
        self.assertEqual(res.name, "red_teams")

    def test_one_unreachable_team_fails_even_if_the_rest_are_fine(self):
        # This is the soak's exact shape: engine-node teams fine, satellite teams dead.
        boxes = self._boxes((221, 2), (225, 2))
        res = self._run(boxes, {"192.168.221.2"})
        self.assertIs(res.status, verify.Status.FAIL)
        self.assertIn("team225", res.detail)

    def test_a_team_with_no_usable_ip_is_not_counted_as_reachable(self):
        boxes = [{"name": "a", "ip": "192.168.221.2"}, {"name": "b", "ip": ""},
                 {"name": "c"}]
        res = self._run(boxes, {"192.168.221.2"})
        self.assertIs(res.status, verify.Status.PASS)
        self.assertIn("1 teams", res.detail)  # only the usable one is proven

    def test_no_ip_bearing_boxes_is_a_skip_never_a_pass(self):
        res = self._run([{"name": "a", "ip": ""}], set())
        self.assertIs(res.status, verify.Status.SKIP_UNAVAILABLE)

    def test_probing_stops_at_the_first_box_that_answers(self):
        # A healthy team must cost one round trip, not one per box — this gate runs
        # before T0 with the whole deploy waiting on it.
        boxes = self._boxes((221, 2), (221, 3), (221, 4))
        calls = []
        res = self._run(boxes, {"192.168.221.2"}, calls=calls)
        self.assertIs(res.status, verify.Status.PASS)
        self.assertEqual(calls, ["192.168.221.2"])

    def test_falls_through_to_the_next_box_before_failing_a_team(self):
        boxes = self._boxes((221, 2), (221, 3))
        calls = []
        res = self._run(boxes, {"192.168.221.3"}, calls=calls)
        self.assertIs(res.status, verify.Status.PASS)
        self.assertEqual(calls, ["192.168.221.2", "192.168.221.3"])

    def test_ssh_spawn_failure_is_a_skip_not_a_fail(self):
        # Unprovable is SKIP in this file's discipline: an operator without a red01
        # ssh key must not read as "routing is broken".
        with patch.object(verify.subprocess, "run", side_effect=OSError("no ssh")):
            res = verify.check_red_teams(self.CTX, self._boxes((221, 2)), "10.0.0.196")
        self.assertIs(res.status, verify.Status.SKIP_UNAVAILABLE)


class ConvertedGates(unittest.TestCase):
    """The D6 conversion itself: these gates return GateResult, not bare bool/None."""

    def test_check_isolation_returns_gate_result(self):
        with patch.object(verify, "ssh_to_engine", return_value=_proc(0, FORWARD_OK, "")):
            result, _ = _run(verify.check_isolation, CTX,
                             {"team1": {"identifier": 104}}, [])
        self.assertIsInstance(result, verify.GateResult)

    def test_check_packet_accounts_returns_gate_result(self):
        result, _ = _run(verify.check_packet_accounts, CTX, {"credentials": {}}, [])
        self.assertIsInstance(result, verify.GateResult)

    def test_check_misconfig_survival_returns_gate_result(self):
        result, _ = _run(verify.check_misconfig_survival, CTX, [])
        self.assertIsInstance(result, verify.GateResult)

    def test_check_plant_coverage_returns_gate_result(self):
        tmp, comp_dir = _comp_dir()
        self.addCleanup(tmp.cleanup)
        _state(comp_dir, {"plant_coverage_failed": {}, "nakon_failed_steps": []})
        result, _ = _run(verify.check_plant_coverage, comp_dir)
        self.assertIsInstance(result, verify.GateResult)

    def test_check_services_returns_gate_results(self):
        session = _Session([{"ID": 1, "Name": "team1"}], {"1": []})
        results, _ = _run(verify.check_services, "http://e", session, {}, False)
        self.assertTrue(results)
        self.assertTrue(all(isinstance(r, verify.GateResult) for r in results))


class MainSummaryWiring(unittest.TestCase):
    """D6 end-to-end: main's SUMMARY and exit code are both derived from the results."""

    def _comp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        comp_dir = Path(tmp.name)
        (comp_dir / "teams.json").write_text(json.dumps(
            {"team1": {"identifier": 104, "password": "pw"}}))
        (comp_dir / "nakon-config.json").write_text(json.dumps({"machines": []}))
        return comp_dir

    def _run_main(self, comp_dir, extra=()):
        patches = [
            patch.object(verify, "read_terraform_ctx",
                         side_effect=verify.CheckError("terraform unavailable")),
            patch.object(verify, "check_logins", return_value=(True, object())),
            patch.object(verify, "check_no_default_creds", return_value=True),
            patch.object(verify, "check_services",
                         return_value=[verify._pass("services", gating=False)]),
            patch.object(verify, "check_isolation", return_value=verify._skip("isolation")),
            patch.object(verify, "report_healthcheck_status"),
            patch.object(verify, "check_misconfig", return_value=True),
            patch.object(verify, "check_misconfig_survival",
                         return_value=verify._pass("misconfig_survival")),
            patch.object(verify, "report_beacons"),
            patch.object(verify, "check_injects", return_value=(False, True)),
            patch.object(verify, "check_round_loop", return_value=verify._pass("round_loop")),
            patch.object(verify, "check_plant_coverage",
                         return_value=verify._skip("plant_coverage")),
            patch.object(verify, "check_domains",
                         return_value=verify._skip("domains", gating=False)),
            patch.object(sys, "argv",
                         ["verify-competition.py", str(comp_dir), "--engine-ip",
                          "10.0.0.1", *extra]),
        ]
        out = io.StringIO()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with contextlib.redirect_stdout(out):
            rc = verify.main()
        return rc, out.getvalue()

    def test_skip_fails_the_exit_code_and_summary_agrees(self):
        comp_dir = self._comp()
        rc, out = self._run_main(comp_dir)
        self.assertEqual(rc, 1)
        self.assertIn(f"{'isolation':<18}: SKIP", out)
        self.assertIn(f"{'plant_coverage':<18}: SKIP", out)
        self.assertIn("RESULT: FAIL", out)

    def test_allow_unverified_makes_the_same_run_pass(self):
        comp_dir = self._comp()
        rc, out = self._run_main(comp_dir, extra=[
            "--allow-unverified", "isolation", "--allow-unverified", "plant_coverage"])
        self.assertEqual(rc, 0)
        self.assertIn(f"{'isolation':<18}: SKIP", out)
        self.assertIn("(allowed — --allow-unverified)", out)
        self.assertIn("RESULT: PASS", out)


if __name__ == "__main__":
    unittest.main()
