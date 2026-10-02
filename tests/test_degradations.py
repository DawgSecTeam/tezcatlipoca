"""Tolerated-failure ledger.

The deploy deliberately continues past some failed prerequisites — each site has its own
reason. The problem was that the fact existed only as a WARNING line in a scrollback:
"apt prep failed", "DNS fix failed", "box never settled". That is how a range comes up
looking green with every service down, and why nakon's silent install failures had no
named cause. Now each one lands in `.deploy_state.json["degradations"]`, gets one summary
at the end of the deploy, and verify reads it back.

Offline throughout.
"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import deploy  # noqa: E402
import hardening_ops  # noqa: E402
import utils  # noqa: E402

# verify-competition.py has a hyphen, so it is loaded by path (same as test_verify_gates).
_SPEC = importlib.util.spec_from_file_location(
    "verify_degradations_test", _REPO / "verify-competition.py")
verify = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify)


class Ledger(unittest.TestCase):
    def setUp(self):
        utils.clear_degradations()
        self.addCleanup(utils.clear_degradations)

    def test_records_and_summarises_with_counts(self):
        utils.record_degradation("apt prep failed", "192.168.1.5: rc=100")
        utils.record_degradation("apt prep failed", "192.168.1.5: rc=100")
        utils.record_degradation("DNS fix failed", "192.168.1.6: timeout")
        summary = utils.degradation_summary()
        by_what = {item["what"]: item for item in summary}
        self.assertEqual(by_what["apt prep failed"]["count"], 2)
        self.assertEqual(by_what["DNS fix failed"]["count"], 1)
        self.assertEqual(len(utils.degradations()), 3)   # raw entries are not collapsed

    def test_entries_carry_a_timestamp_and_bounded_detail(self):
        entry = utils.record_degradation("x", "y" * 1000)
        self.assertTrue(entry["at"])
        self.assertLessEqual(len(entry["detail"]), 300)

    def test_clearing_empties_the_ledger(self):
        utils.record_degradation("x")
        utils.clear_degradations()
        self.assertEqual(utils.degradations(), [])
        self.assertEqual(utils.degradation_summary(), [])


class DeployPersists(unittest.TestCase):
    def setUp(self):
        utils.clear_degradations()
        self.addCleanup(utils.clear_degradations)

    def _ctx(self, comp_dir):
        return types.SimpleNamespace(state={}, comp_name="probe", comp_dir=comp_dir)

    def test_nothing_recorded_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = self._ctx(Path(d))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                deploy._record_degradations(ctx)
        self.assertNotIn("degradations", ctx.state)
        self.assertNotIn("tolerated", out.getvalue())

    def test_recorded_entries_land_in_state_and_are_printed_once(self):
        utils.record_degradation("apt prep failed", "box1 rc=100")
        utils.record_degradation("apt prep failed", "box1 rc=100")
        with tempfile.TemporaryDirectory() as d:
            ctx = self._ctx(Path(d))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                deploy._record_degradations(ctx)
        self.assertEqual(len(ctx.state["degradations"]), 2)
        text = out.getvalue()
        self.assertIn("2 tolerated failure(s)", text)
        self.assertIn("apt prep failed x2", text)      # collapsed in the summary
        self.assertIn("verify-competition", text)      # points at the reader


class VerifySurfaces(unittest.TestCase):
    def setUp(self):
        utils.clear_degradations()
        self.addCleanup(utils.clear_degradations)

    def _state(self, d, payload):
        (Path(d) / ".deploy_state.json").write_text(json.dumps(payload))

    def test_missing_state_skips_without_gating(self):
        with tempfile.TemporaryDirectory() as d:
            with contextlib.redirect_stdout(io.StringIO()):
                result = verify.check_degradations(Path(d))
        self.assertFalse(result.gating)

    def test_not_recorded_is_distinguished_from_recorded_and_clean(self):
        with tempfile.TemporaryDirectory() as d:
            self._state(d, {})
            with contextlib.redirect_stdout(io.StringIO()) as out:
                result = verify.check_degradations(Path(d))
            self.assertIn("not recorded", result.detail)
            self.assertIn("SKIP", out.getvalue())
            self._state(d, {"degradations": []})
            with contextlib.redirect_stdout(io.StringIO()) as out:
                result = verify.check_degradations(Path(d))
            self.assertIn("no tolerated failures", out.getvalue())
            self.assertEqual(result.status, verify.Status.PASS)

    def test_recorded_entries_are_listed_and_do_not_fail_the_run(self):
        with tempfile.TemporaryDirectory() as d:
            self._state(d, {"degradations": [
                {"what": "apt prep failed", "detail": "box1: rc=100", "at": "now"},
                {"what": "apt prep failed", "detail": "box1: rc=100", "at": "now"},
                {"what": "DNS fix failed", "detail": "box2: timeout", "at": "now"},
            ]})
            with contextlib.redirect_stdout(io.StringIO()) as out:
                result = verify.check_degradations(Path(d))
        text = out.getvalue()
        self.assertIn("apt prep failed", text)
        self.assertIn("DNS fix failed", text)
        self.assertEqual(result.status, verify.Status.PASS)   # the deploy survived them
        self.assertIn("apt prep failed x2", result.detail)


class TheSiteActuallyRecords(unittest.TestCase):
    """The wiring is the point: the ledger is worthless if no site feeds it."""

    def setUp(self):
        utils.clear_degradations()
        self.addCleanup(utils.clear_degradations)

    def test_a_failed_apt_prep_records_a_degradation(self):
        target = {"ip": "192.168.1.5", "vmid": 1150, "identifier": 210}
        failed = types.SimpleNamespace(returncode=1, stdout="", stderr="E: lock held")
        with patch.dict("os.environ", {"TF_VAR_proxmox_node": "pve"}), \
                patch.object(hardening_ops.subprocess, "run", return_value=failed), \
                patch.object(hardening_ops.time, "sleep"), \
                patch.object(hardening_ops, "guest_agent_exec_root",
                             return_value=(100, "", "no space left")), \
                patch.object(hardening_ops, "gateway_proxy", return_value=None), \
                patch.object(hardening_ops, "wait_for_guest_agent", return_value=False), \
                patch.object(hardening_ops, "wait_boxes_settled", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            hardening_ops.prep_apt_on_boxes([target], {"ssh_key_path": "/k"})
        whats = [entry["what"] for entry in utils.degradations()]
        self.assertIn("apt prep failed", whats)
        detail = " ".join(entry["detail"] for entry in utils.degradations())
        self.assertIn("192.168.1.5", detail)


if __name__ == "__main__":
    unittest.main()
