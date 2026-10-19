"""No nested agent wait may consume the whole enclosing deadline in one uninterruptible call: every probe is bounded by AGENT_POLL_CAP and the remaining budget."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from common import windows_ops  # noqa: E402


class FakeClock:
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
            clock.now += timeout
            return False

        with patch.object(windows_ops, "time", clock), \
                patch.object(windows_ops, "wait_for_guest_agent", side_effect=fake_agent):
            ok = windows_ops._wait_for_windows_setup_complete("pve", 221, deadline)

        self.assertFalse(ok)
        self.assertTrue(calls, "the loop must keep probing, not give up after one call")
        self.assertLessEqual(max(calls), windows_ops.AGENT_POLL_CAP,
                             "one agent call consumed more than the poll cap")
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
            return state["n"] >= 3

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





if __name__ == "__main__":
    unittest.main()
