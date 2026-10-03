"""No agent wait may outlive the deadline that is waiting on it (C3) — scale8 soak 2026-10-02.

The soak's phase-6 resume hung ~40 minutes in `wait_for_windows_sshd`. The cause was one
level down: `_wait_for_windows_setup_complete` passed `wait_for_guest_agent` the ENTIRE
remaining budget (`timeout=max(1, int(deadline - time.time()))` against a 900 s default),
so a host whose agent never answered consumed the whole window in a single uninterruptible
call and every enclosing timeout was decorative.

Two invariants, both testable with a fake clock:
  - each nested agent call is bounded by AGENT_POLL_CAP and by the remaining budget;
  - a wedged agent still terminates at the helper's own deadline, and says so.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import windows_ops  # noqa: E402


class FakeClock:
    """`time.time()`/`time.sleep()` that advance instead of waiting."""

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class NestedWaitIsBounded(unittest.TestCase):
    def test_setup_wait_never_hands_the_whole_budget_to_one_call(self):
        clock = FakeClock()
        calls = []
        deadline = clock.now + 900          # bootstrap_windows_box's default budget

        def fake_agent(node, vmid, timeout=None):
            calls.append(timeout)
            clock.now += timeout            # a wedged agent burns its whole timeout
            return False

        with patch.object(windows_ops, "time", clock), \
                patch.object(windows_ops, "wait_for_guest_agent", side_effect=fake_agent):
            ok = windows_ops._wait_for_windows_setup_complete("pve", 221, deadline)

        self.assertFalse(ok)
        self.assertTrue(calls, "the loop must keep probing, not give up after one call")
        self.assertLessEqual(max(calls), windows_ops.AGENT_POLL_CAP,
                             "one agent call consumed more than the poll cap")
        # ...and it stopped at the deadline rather than running past it.
        self.assertLessEqual(clock.now, deadline + windows_ops.AGENT_POLL_CAP)

    def test_a_slow_but_answering_box_still_completes(self):
        clock = FakeClock()
        calls = []
        deadline = clock.now + 900
        state = {"n": 0}

        def fake_agent(node, vmid, timeout=None):
            calls.append(timeout)
            clock.now += 30
            state["n"] += 1
            return state["n"] >= 3          # answers on the third poll

        def fake_exec(node, vmid, script, timeout=None):
            calls.append(timeout)
            return (0, "IMAGE_STATE_COMPLETE", "")

        with patch.object(windows_ops, "time", clock), \
                patch.object(windows_ops, "wait_for_guest_agent", side_effect=fake_agent), \
                patch.object(windows_ops, "guest_agent_exec_windows", side_effect=fake_exec):
            self.assertTrue(windows_ops._wait_for_windows_setup_complete("pve", 221, deadline))

    def test_probe_timeouts_shrink_with_the_remaining_budget(self):
        clock = FakeClock()
        seen = []

        def fake_exec(node, vmid, script, timeout=None):
            seen.append(timeout)
            clock.now += timeout
            raise RuntimeError("agent wedged")

        # 100 s of budget left: the first probe may ask for at most that, not 60 s blindly
        # forever — the point is that no call can outlive the enclosing loop.
        with patch.object(windows_ops, "time", clock), \
                patch.object(windows_ops, "guest_agent_exec_windows", side_effect=fake_exec):
            self.assertFalse(windows_ops.wait_for_windows_sshd("pve", 221, timeout=100))
        self.assertLessEqual(max(seen), windows_ops.AGENT_POLL_CAP)

    def test_wedged_sshd_wait_terminates_and_says_the_agent_failed(self):
        clock = FakeClock()
        lines = []

        def fake_exec(node, vmid, script, timeout=None):
            clock.now += timeout
            raise RuntimeError("500 Server Error: agent/exec")

        with patch.object(windows_ops, "time", clock), \
                patch.object(windows_ops, "guest_agent_exec_windows", side_effect=fake_exec), \
                patch("builtins.print", side_effect=lambda *a, **k: lines.append(" ".join(map(str, a)))):
            ok = windows_ops.wait_for_windows_sshd("pve", 221, timeout=180)

        self.assertFalse(ok)
        self.assertTrue(any("agent probe(s) failed" in l for l in lines),
                        f"a wedged agent must be diagnosable, got: {lines}")

    def test_wedged_adws_wait_terminates_and_says_the_agent_failed(self):
        clock = FakeClock()
        lines = []

        def fake_exec(node, vmid, script, timeout=None):
            clock.now += timeout
            raise RuntimeError("500 Server Error: agent/exec")

        with patch.object(windows_ops, "time", clock), \
                patch.object(windows_ops, "guest_agent_exec_windows", side_effect=fake_exec), \
                patch("builtins.print", side_effect=lambda *a, **k: lines.append(" ".join(map(str, a)))):
            self.assertIsNone(windows_ops.wait_for_adws("pve", 221, timeout=180))
        self.assertTrue(any("agent probe(s) failed" in l for l in lines))


class BoundedHelper(unittest.TestCase):
    def test_never_returns_less_than_a_second(self):
        self.assertEqual(windows_ops._bounded(60, 0), 1)
        self.assertEqual(windows_ops._bounded(60, -5), 1)

    def test_smallest_wins(self):
        self.assertEqual(windows_ops._bounded(60, 30), 30)
        self.assertEqual(windows_ops._bounded(20, 9999), 20)
        self.assertEqual(windows_ops._bounded(9999, 9999), windows_ops.AGENT_POLL_CAP)


if __name__ == "__main__":
    unittest.main()
