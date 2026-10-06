"""The resume budget: refuse a resume-loop instead of repeating it.

`docs/e2e-testing.md` has always said "max 2 repair-resume cycles; a third consecutive
resume is not a repair, it's a resume-loop", but that lived only in prose. cde-2026
burned eleven attempts (deploy6 -> deploy16, 2026-09-29/30) on one Windows golden,
eight of them consecutive and all phase 4. These tests pin the enforceable version:

  * the same phase failing the same way twice refuses the third resume, *before* any
    infrastructure work, and names the destroy-and-redeploy path;
  * a DIFFERENT failure at the same phase resets the budget — that is new information,
    not a loop;
  * `--force-from-phase` still overrides (an operator who fixed the cause must not be
    locked out by their own guard).

Offline; no Proxmox, terraform or SSH.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from deploy_lib import failure as dl_failure  # noqa: E402
from deploy_lib import gates as dl_gates  # noqa: E402
from constants import RESUME_ATTEMPT_LIMIT  # noqa: E402


class FailureSignature(unittest.TestCase):
    def test_volatile_parts_are_normalised_away(self):
        a = dl_failure.failure_signature(RuntimeError("vmid 1232 did not come up in 900s"))
        b = dl_failure.failure_signature(RuntimeError("vmid 1444 did not come up in 1800s"))
        self.assertEqual(a, b, "the same failure on a different box must match")

    def test_uuid_and_hex_ids_are_normalised(self):
        a = dl_failure.failure_signature(
            RuntimeError("task UPID:pve:0001A2B3C4D5 failed 9f8e7d6c-1111-2222-3333-444455556666"))
        b = dl_failure.failure_signature(
            RuntimeError("task UPID:pve:0009F8E7D6C5 failed aaaa1111-2222-3333-4444-555566667777"))
        self.assertEqual(a, b)

    def test_different_failures_do_not_collide(self):
        a = dl_failure.failure_signature(RuntimeError("golden did not boot"))
        b = dl_failure.failure_signature(RuntimeError("template not found"))
        self.assertNotEqual(a, b)

    def test_exception_type_is_part_of_the_signature(self):
        a = dl_failure.failure_signature(RuntimeError("same text"))
        b = dl_failure.failure_signature(ValueError("same text"))
        self.assertNotEqual(a, b)

    def test_signature_is_bounded_and_single_line(self):
        sig = dl_failure.failure_signature(RuntimeError("first line\nsecond line\n" + "x" * 500))
        self.assertLessEqual(len(sig), 160)
        self.assertNotIn("\n", sig)


class StreakRecording(unittest.TestCase):
    def test_first_failure_starts_the_streak(self):
        state = {}
        self.assertEqual(dl_failure.record_failure(state, 4, RuntimeError("boom 1")), 1)
        self.assertEqual(state["failure_streak"]["phase"], 4)

    def test_same_phase_and_signature_increments(self):
        state = {}
        dl_failure.record_failure(state, 4, RuntimeError("vmid 1 timeout"))
        self.assertEqual(dl_failure.record_failure(state, 4, RuntimeError("vmid 2 timeout")), 2)

    def test_a_different_signature_at_the_same_phase_resets(self):
        state = {}
        dl_failure.record_failure(state, 4, RuntimeError("disk full"))
        self.assertEqual(dl_failure.record_failure(state, 4, RuntimeError("template missing")), 1)

    def test_the_same_signature_at_a_different_phase_resets(self):
        state = {}
        dl_failure.record_failure(state, 4, RuntimeError("boom"))
        self.assertEqual(dl_failure.record_failure(state, 6, RuntimeError("boom")), 1)

    def test_clearing_removes_the_streak(self):
        state = {}
        dl_failure.record_failure(state, 4, RuntimeError("boom"))
        dl_failure.clear_failure_streak(state)
        self.assertNotIn("failure_streak", state)


class ResumeBudgetGuard(unittest.TestCase):
    def _state(self, count, phase=4, signature="RuntimeError: boom"):
        return {"failure_streak": {"phase": phase, "signature": signature, "count": count}}

    def test_first_repeat_is_allowed(self):
        dl_gates.guard_resume_streak(4, self._state(1))          # resume #2 is fine

    def test_limit_reached_refuses_with_the_recovery_path(self):
        with self.assertRaises(SystemExit) as ctx:
            dl_gates.guard_resume_streak(4, self._state(RESUME_ATTEMPT_LIMIT))
        message = str(ctx.exception)
        self.assertIn("resume-loop", message)
        self.assertIn("destroy-competition.py", message)      # the path that helps
        self.assertIn("create-competition.py", message)
        self.assertIn("--force-from-phase", message)          # the escape hatch, named

    def test_a_streak_at_another_phase_does_not_block_this_one(self):
        dl_gates.guard_resume_streak(6, self._state(RESUME_ATTEMPT_LIMIT, phase=4))

    def test_force_overrides(self):
        dl_gates.guard_resume_streak(4, self._state(99), force=True)

    def test_missing_or_malformed_state_is_not_a_refusal(self):
        dl_gates.guard_resume_streak(4, {})
        dl_gates.guard_resume_streak(4, {"failure_streak": None})
        dl_gates.guard_resume_streak(4, {"failure_streak": {"phase": 4, "count": None}})


class LoadGate(unittest.TestCase):
    """`--min-load-free`: the wait operators were doing by hand."""

    def setUp(self):
        import pve_api as range_ops  # the owning module (range_ops is a facade)
        self.range_ops = range_ops

    def test_waits_until_the_node_falls_below_the_threshold(self):
        loads = iter([32.06, 28.0, 9.5])
        slept = []
        with patch.object(self.range_ops, "node_loadavg",
                                        side_effect=lambda _n: next(loads)), \
                patch.object(self.range_ops, "time") as fake_time:
            fake_time.time.return_value = 0        # never hits the deadline
            ok = self.range_ops.wait_for_node_load("proxmox", 10, timeout=3600,
                                                   sleep=slept.append)
        self.assertTrue(ok)
        self.assertEqual(slept, [30, 30])          # one wait per above-threshold reading

    def test_proceeds_after_the_deadline_rather_than_hanging(self):
        with patch.object(self.range_ops, "node_loadavg", return_value=99.0), \
                patch.object(self.range_ops, "time") as fake_time:
            fake_time.time.side_effect = [0, 99999]
            ok = self.range_ops.wait_for_node_load("proxmox", 10, timeout=60,
                                                   sleep=lambda _s: None)
        self.assertFalse(ok)                        # caller decides; we do not hang forever

    def test_a_node_that_will_not_report_load_does_not_block_the_deploy(self):
        with patch.object(self.range_ops, "node_loadavg", return_value=None):
            self.assertFalse(self.range_ops.wait_for_node_load("proxmox", 10, timeout=60,
                                                               sleep=lambda _s: None))

    def test_node_loadavg_reads_the_first_of_the_three_values(self):
        calls = []

        def fake_api(method, path, **kw):
            calls.append((method, path))
            return {"data": {"loadavg": [3.5, 4.0, 4.5]}}

        with patch.object(self.range_ops, "proxmox_api", fake_api):
            self.assertEqual(self.range_ops.node_loadavg("proxmox"), 3.5)
        self.assertEqual(calls, [("GET", "/nodes/proxmox/status")])

    def test_node_loadavg_returns_none_when_the_api_is_down(self):
        def dead(*a, **kw):
            raise RuntimeError("unreachable")

        with patch.object(self.range_ops, "proxmox_api", dead):
            self.assertIsNone(self.range_ops.node_loadavg("proxmox"))


if __name__ == "__main__":
    unittest.main()
