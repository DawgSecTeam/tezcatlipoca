"""The deploy-time verify is labelled INTERIM wherever its result is read.

It runs before red/blue staging, and staging is what registers the pins: caught live the
same `pins_registered` gate read FAIL at deploy time and PASS three minutes later with
everything else unchanged, so one run's evidence held two contradictory results for one gate.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import verifier.runner as v_runner  # noqa: E402
from artifacts_lib import report as a_report  # noqa: E402
from scrim import fire_test  # noqa: E402


def _args(interim):
    return SimpleNamespace(interim=interim, packet_profile=None, strict_services=False,
                           red_identity=False, red_teams="own", expect_no_vulns=False,
                           fix_round_loop=False)


class GateEmission(unittest.TestCase):
    def _run(self, interim):
        # The label is emitted before the first gate, so stopping at that gate is the whole
        # assertion: no gate is allowed to run in this test.
        buf = io.StringIO()
        with mock.patch.object(v_runner.scoreboard, "check_logins",
                               side_effect=RuntimeError("stop before the first gate")), \
                contextlib.redirect_stdout(buf):
            with self.assertRaises(RuntimeError):
                v_runner.run_gates(_args(interim), Path("/nonexistent"), {}, {}, "pw", [],
                                   "http://engine")
        return buf.getvalue()

    def test_interim_run_labels_its_gate_emission(self):
        out = self._run(True)
        self.assertIn("INTERIM VERIFY", out)
        self.assertIn("pins_registered", out)

    def test_a_normal_run_is_not_labelled(self):
        self.assertNotIn("INTERIM VERIFY", self._run(False))


class DeployTimeInvocation(unittest.TestCase):
    def test_the_deploy_verify_asks_for_the_interim_label(self):
        seen = []
        creds = {"ENGINE_IP": "10.0.0.9", "ADMIN_PW": "pw", "TEAM1_ID": "101",
                 "KEY_PATH": "/tmp/key", "BOX_USER": "ubuntu", "VM_USER": "sysadmin"}
        with mock.patch.object(fire_test.procs, "run",
                               side_effect=lambda cmd, **k: seen.append(cmd) or
                               SimpleNamespace(returncode=0, stdout="", stderr="")), \
                mock.patch.object(fire_test, "write_verify_log"), \
                mock.patch.object(fire_test, "run_fire_test"):
            fire_test.stage_verify(SimpleNamespace(run_dir=None), Path("/nonexistent"), creds)
        self.assertIn("--interim", seen[0])


class ReportVerifyLine(unittest.TestCase):
    def test_the_recorded_reading_reaches_the_report_interim(self):
        # The scrim records each verify's outcome next to its log; the last one written (the
        # pre-T0 authoritative verify) is the record a finished run keeps, so a run that dies
        # between the deploy-time verify and staging keeps the interim label.
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            test_dir = Path(tmp) / "test"
            test_dir.mkdir()
            args = SimpleNamespace(run_dir=str(run_dir), test_dir=str(test_dir))
            with mock.patch.object(fire_test.core, "secure_evidence", lambda p: None):
                fire_test.write_verify_log(args, "interim (pre-staging)",
                                           SimpleNamespace(returncode=1, stdout="", stderr=""))
            manifest = json.loads((test_dir / "test.json").read_text())
            text = a_report.render_report_skeleton(test_dir)
        self.assertTrue(manifest["verify"]["interim"])
        self.assertIn("interim deploy-time verify-competition.py exit 1", text)

    def test_the_report_marks_the_interim_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_path = Path(tmp)
            (test_path / "test.json").write_text(json.dumps(
                {"verify": {"exit_code": 1, "interim": True, "log": "verify.log"}}))
            text = a_report.render_report_skeleton(test_path)
        self.assertIn("interim deploy-time verify-competition.py exit 1", text)

    def test_reports_an_authoritative_verify_plainly(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_path = Path(tmp)
            (test_path / "test.json").write_text(json.dumps(
                {"verify": {"exit_code": 1, "log": "verify.log"}}))
            text = a_report.render_report_skeleton(test_path)
        self.assertIn("| (verify) | verify-competition.py exit 1", text)

if __name__ == "__main__":
    unittest.main()
