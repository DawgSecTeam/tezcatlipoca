"""A3 — declared telemetry must be PROVEN fresh pre-T0, per team.

run-14ac7f14 came within minutes of a 4-team event with zero windows events on any
head; nothing in verify could notice. telemetry.json declares feeds; the gate asks
each team's own box to query its ingest API over a freshness window; undeclared =
informational skip, declared-and-stale = blocking FAIL."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from verifier import telemetry  # noqa: E402
from verifier.model import CheckError, Status  # noqa: E402

TEAMS = {"team1": {"identifier": "130"}, "team2": {"identifier": "131"}}
BOXES = [{"name": "splunk01", "last_octet": 2}]
EXPECT = [{"box": "splunk01", "label": "windows_evtlog fresh",
           "url": "https://localhost:8089/services/search/jobs/oneshot",
           "auth_user": "admin", "auth_password": "pw!",
           "params": {"search": "search index=windows_evtlog | stats count as n",
                      "earliest_time": "-15m", "output_mode": "json"},
           "count_field": "n", "min_count": 1}]


def _run(comp_dir, stdout='{"results":[{"n":"7"}]}', raises=None):
    def fake(ctx, ip, cmd, timeout=60):
        if raises:
            raise raises
        calls.append((ip, cmd))
        return SimpleNamespace(stdout=stdout)
    calls = []
    with mock.patch.object(telemetry.context, "ssh_via_gateway", fake):
        result = telemetry.check_telemetry({}, comp_dir, TEAMS, BOXES)
    return result, calls


class TelemetryGate(unittest.TestCase):
    def test_no_declaration_skips_without_gating(self):
        with tempfile.TemporaryDirectory() as td:
            result, _ = _run(Path(td))
        self.assertIs(result.status, Status.SKIP_UNAVAILABLE)
        self.assertFalse(result.gating)

    def test_fresh_feeds_pass_for_every_team(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "telemetry.json").write_text(json.dumps({"expected": EXPECT}))
            result, calls = _run(Path(td))
            self.assertIs(result.status, Status.PASS)
            self.assertTrue(result.gating)
            self.assertEqual([ip for ip, _ in calls], ["192.168.130.2", "192.168.131.2"])
            self.assertIn("--data-urlencode", calls[0][1])
            self.assertIn("earliest_time=-15m", calls[0][1])

    def test_zero_count_is_blocking_fail_with_the_offending_label(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "telemetry.json").write_text(json.dumps({"expected": EXPECT}))
            result, _ = _run(Path(td), stdout='{"results":[{"n":"0"}]}')
            self.assertIs(result.status, Status.FAIL)
            self.assertIn("team1", result.detail)
            self.assertIn("windows_evtlog fresh", result.detail)

    def test_ssh_refusal_reads_as_failure_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "telemetry.json").write_text(json.dumps({"expected": EXPECT}))
            result, _ = _run(Path(td), raises=CheckError("ssh refused"))
            self.assertIs(result.status, Status.FAIL)
            self.assertIn("ssh refused", result.detail)

    def test_malformed_declaration_raises_check_error_for_the_runner_to_gate(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "telemetry.json").write_text("{not json")
            with self.assertRaises(CheckError):
                telemetry.load_expectations(Path(td))


class SummaryParse(unittest.TestCase):
    def test_informational_fails_are_not_hard_gates(self):
        from scrim.red_stage import _hard_gate_failures
        stdout = ("  services          : PASS  all up\n"
                  "  firewall_in_path  : FAIL  nope [informational]\n"
                  "  telemetry         : FAIL  2/8 feeds not fresh — team1:x count=0\n"
                  "  injects           : SKIP  competition ships no injects/ dir\n")
        bad = _hard_gate_failures(stdout)
        self.assertEqual(len(bad), 1)
        self.assertIn("telemetry", bad[0])
        self.assertEqual(_hard_gate_failures(stdout, {"telemetry"}), [])


if __name__ == "__main__":
    unittest.main()


class ScrimAbortsOnGatingFail(unittest.TestCase):
    """The authoritative verify's non-informational FAILs refuse the event (A3)."""

    def _verify_run(self, stdout, rc=0, allow=""):
        from argparse import Namespace
        from unittest import mock
        from scrim import red_stage
        r = SimpleNamespace(stdout=stdout, stderr="", returncode=rc)
        with mock.patch("scrim.red_stage.procs.run", return_value=r), \
             mock.patch("scrim.compworld.verify_cmd", return_value=["cmd"]), \
             mock.patch("scrim.red_stage.fire_test.write_verify_log"):
            args = Namespace(red_ip="10.0.0.99", allow_gate_fail=allow)
            red_stage.verify_red_reaches_teams(args, SimpleNamespace(name="c1"), {})

    def _both_red_pass(self, extra=""):
        return ("  red_identity      : PASS  c2 id seen\n"
                "  red_teams         : PASS  all reachable\n" + extra)

    def test_telemetry_fail_raises_even_when_red_passes(self):
        with self.assertRaises(RuntimeError) as cm:
            self._verify_run(self._both_red_pass(
                "  telemetry         : FAIL  8/8 feeds not fresh\n"))
        self.assertIn("telemetry", str(cm.exception))

    def test_allow_gate_fail_waives_named_label_only(self):
        self._verify_run(self._both_red_pass(
            "  telemetry         : FAIL  8/8 feeds not fresh\n"), allow="telemetry")
        with self.assertRaises(RuntimeError):
            self._verify_run(self._both_red_pass(
                "  injects           : FAIL  0 published\n"), allow="telemetry")

    def test_informational_fail_never_aborts(self):
        self._verify_run(self._both_red_pass(
            "  firewall_in_path  : FAIL  nope [informational]\n"))
