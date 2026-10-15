"""A freshly cloned box is not settled until cloud-init is done: the settle waiter now
gates on `cloud-init status` before any long apt/build session opens, bounded, and fails
loudly (rather than racing sshd) when cloud-init is still running past the bound.

Offline: the ssh entry point is injected, time is a fake clock.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from guests import box_settle_ops  # noqa: E402

CTX = {"ssh_key_path": "/k", "scoring_engine_ip": "10.0.0.9", "box_username": "ubuntu"}
TARGET = {"ip": "192.168.1.5", "vmid": 1150}


class FakeClock:
# `time.time()`/`time.sleep()` that advance instead of waiting.

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _reply(rc, stdout):
    return type("R", (), {"returncode": rc, "stdout": stdout, "stderr": ""})()


class Verdict(unittest.TestCase):
    def test_rc_is_authoritative(self):
        self.assertEqual(box_settle_ops._cloud_init_verdict(0, "status: done"), "done")
        self.assertEqual(box_settle_ops._cloud_init_verdict(2, "status: error"), "error")

    def test_text_settles_a_state_when_rc_disagrees(self):
        self.assertEqual(box_settle_ops._cloud_init_verdict(1, "status: done"), "done")
        self.assertEqual(box_settle_ops._cloud_init_verdict(0, "status: error"), "error")

    def test_rc_two_does_not_outvote_a_named_text_state(self):
        """cloud-init returns 2 for a boot that FINISHED with recorded errors, so on an
        image that always has one rc 2 arrived with `status: done` on every box — read
        rc-first, the gate recorded a tolerated failure for a finished boot (which
        hard-fails verify's degradations gate) and skipped its own wait when the text
        said `status: running` (live 2026-10-15, run-b1bdb04a)."""
        self.assertEqual(box_settle_ops._cloud_init_verdict(2, "status: done"), "done")
        self.assertEqual(box_settle_ops._cloud_init_verdict(2, "status: running"), "running")
        self.assertEqual(box_settle_ops._cloud_init_verdict(2, "status: disabled"), "done")
        # ...and rc still decides when the text names no state at all
        self.assertEqual(box_settle_ops._cloud_init_verdict(2, ""), "error")

    def test_disabled_counts_as_done(self):
        # Nothing will ever run again — there is no apt pass left to race.
        self.assertEqual(box_settle_ops._cloud_init_verdict(1, "status: disabled"), "done")

    def test_anything_else_is_still_running(self):
        self.assertEqual(box_settle_ops._cloud_init_verdict(1, "status: running"), "running")
        self.assertEqual(box_settle_ops._cloud_init_verdict(None, ""), "running")


class WaitDecisions(unittest.TestCase):
    def test_done_returns_without_sleeping(self):
        clock = FakeClock()
        calls = []

        def run(ctx, ip, cmd, **kw):
            calls.append((ip, cmd))
            return _reply(0, "status: done")

        with patch.object(box_settle_ops, "time", clock):
            box_settle_ops.wait_boxes_cloud_init(CTX, [TARGET], timeout=600, run=run)

        self.assertEqual(len(calls), 1)
        self.assertEqual(clock.sleeps, [])
        # The probe guards a non-cloud-init box instead of letting `command not found`
        # read as 'running' for the whole bound.
        self.assertIn("command -v cloud-init", calls[0][1])

    def test_running_then_done_keeps_polling(self):
        clock = FakeClock()
        state = {"n": 0}

        def run(ctx, ip, cmd, **kw):
            state["n"] += 1
            return _reply(0 if state["n"] >= 3 else 1,
                          "status: done" if state["n"] >= 3 else "status: running")

        with patch.object(box_settle_ops, "time", clock):
            box_settle_ops.wait_boxes_cloud_init(CTX, [TARGET], timeout=600, run=run)

        self.assertEqual(state["n"], 3)
        self.assertEqual(clock.sleeps, [box_settle_ops._CLOUD_INIT_POLL] * 2)

    def test_error_warns_and_proceeds(self):
        clock = FakeClock()
        recorded = []

        def run(ctx, ip, cmd, **kw):
            return _reply(2, "status: error\nTraceback: boom")

        with patch.object(box_settle_ops, "time", clock), \
                patch.object(box_settle_ops, "record_degradation",
                             side_effect=lambda what, detail="": recorded.append((what, detail))):
            box_settle_ops.wait_boxes_cloud_init(CTX, [TARGET], timeout=600, run=run)

        self.assertEqual([w for w, _ in recorded], ["cloud-init errored"])
        self.assertIn("status: error", recorded[0][1])

    def test_a_wedged_probe_is_not_a_pass_and_terminates_at_the_bound(self):
        clock = FakeClock()
        seen = []

        def run(ctx, ip, cmd, **kw):
            seen.append(cmd)
            raise RuntimeError("ssh exit 255")

        with patch.object(box_settle_ops, "time", clock):
            with self.assertRaises(RuntimeError) as cm:
                box_settle_ops.wait_boxes_cloud_init(CTX, [TARGET], timeout=600, run=run)

        self.assertIn("still running after 600s", str(cm.exception))
        self.assertLessEqual(clock.now, 1000.0 + 600 + box_settle_ops._CLOUD_INIT_POLL)

    def test_still_running_past_the_bound_fails_loudly_with_the_tail(self):
        clock = FakeClock()

        def run(ctx, ip, cmd, **kw):
            return _reply(1, "status: running\nextended_status: boot")

        with patch.object(box_settle_ops, "time", clock):
            with self.assertRaises(RuntimeError) as cm:
                box_settle_ops.wait_boxes_cloud_init(CTX, [TARGET], timeout=600, run=run)

        message = str(cm.exception)
        self.assertIn("still running after 600s", message)
        self.assertIn("status: running", message)


class SettleWaiterCarriesTheGuarantee(unittest.TestCase):
    def test_settle_waits_for_cloud_init_first_and_forwards_the_bound(self):
        with patch.object(box_settle_ops, "wait_boxes_cloud_init") as gate, \
                patch.object(box_settle_ops, "_box_settled", return_value=True):
            unsettled = box_settle_ops.wait_boxes_settled(
                [TARGET], "pve", cloud_init_ctx=CTX, cloud_init_timeout=123)

        self.assertEqual(unsettled, set())
        gate.assert_called_once_with(CTX, [TARGET], timeout=123)

    def test_cloud_init_ctx_is_keyword_only_and_required(self):
        with self.assertRaises(TypeError):
            box_settle_ops.wait_boxes_settled([TARGET], "pve")


if __name__ == "__main__":
    unittest.main()
