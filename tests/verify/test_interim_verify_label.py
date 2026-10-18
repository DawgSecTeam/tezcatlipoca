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

_REPO = Path(__file__).resolve().parents[2]
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



if __name__ == "__main__":
    unittest.main()
