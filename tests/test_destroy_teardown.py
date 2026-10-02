"""`terraform destroy` is bounded per attempt, and a timeout is a retryable failure.

D2: destroy-competition.py's 4-attempt recovery loop called
run_terraform(["destroy", ...], check=False) with no timeout, so utils.run_terraform
waited on proc.wait(timeout=None) forever (utils.py:233). The documented hang mode is a
Windows DC whose guest agent is down holding the qm lock mid-destroy
(see pre_stop_windows_boxes / pfsense-rvb 6m+ "Still destroying"). While hung, the
operator never reaches the clear_stale_state_lock / sweep_tagged_leftovers recovery and
the tool prints nothing at all. Every `apply` in the pipeline already passed a timeout;
destroy was the one unbounded call.

Offline: run_terraform, the lock recovery, and the leftover sweep are all patched."""

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, _REPO / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


destroy = load_module("destroy_competition_for_timeout_test", "destroy-competition.py")


def _timeout():
    return subprocess.TimeoutExpired(cmd=["terraform", "destroy"], timeout=1800)


class DestroyAttemptTimeoutTests(unittest.TestCase):
    def _run(self, side_effect):
        with patch.object(destroy, "run_terraform", side_effect=side_effect) as p_run, \
             patch.object(destroy, "clear_stale_state_lock") as p_lock, \
             patch.object(destroy, "sweep_tagged_leftovers") as p_sweep:
            result = destroy.destroy_with_recovery(
                {}, "terraform", ["pve"], "cde-2026")
        return result, p_run, p_lock, p_sweep

    def test_every_attempt_passes_the_bounded_timeout(self):
        result, p_run, _p_lock, _p_sweep = self._run([MagicMock(returncode=0)])
        self.assertTrue(result)
        p_run.assert_called_once()
        self.assertEqual(p_run.call_args.kwargs["timeout"],
                         destroy.DESTROY_ATTEMPT_TIMEOUT_S)
        self.assertIsNotNone(destroy.DESTROY_ATTEMPT_TIMEOUT_S)
        self.assertGreater(destroy.DESTROY_ATTEMPT_TIMEOUT_S, 0)

    def test_timeout_is_retried_instead_of_escaping(self):
        """A hung attempt must fall through to the next one — the whole point of the
        4-attempt loop is that recovery runs BETWEEN attempts, and that is unreachable
        if TimeoutExpired propagates out of main()."""
        result, p_run, p_lock, p_sweep = self._run(
            [_timeout(), MagicMock(returncode=0)])
        self.assertTrue(result)
        self.assertEqual(p_run.call_count, 2)
        # Attempt 2 begins with the documented recovery steps.
        p_lock.assert_called_once_with("terraform")
        p_sweep.assert_called_once_with(["pve"], "cde-2026")
        # And the timeout was re-applied to the retry, not only the first attempt.
        self.assertEqual([c.kwargs["timeout"] for c in p_run.call_args_list],
                         [destroy.DESTROY_ATTEMPT_TIMEOUT_S] * 2)

    def test_all_four_timeouts_exhaust_the_loop_and_report_failure(self):
        result, p_run, p_lock, p_sweep = self._run([_timeout()] * 4)
        self.assertFalse(result)
        self.assertEqual(p_run.call_count, 4)
        # Recovery runs before attempts 2, 3 and 4.
        self.assertEqual(p_lock.call_count, 3)
        self.assertEqual(p_sweep.call_args_list,
                         [call(["pve"], "cde-2026")] * 3)

    def test_failed_attempt_still_runs_recovery_before_the_retry(self):
        result, p_run, p_lock, p_sweep = self._run(
            [MagicMock(returncode=1), MagicMock(returncode=0)])
        self.assertTrue(result)
        self.assertEqual(p_run.call_count, 2)
        p_lock.assert_called_once_with("terraform")
        p_sweep.assert_called_once_with(["pve"], "cde-2026")

    def test_success_on_the_first_attempt_skips_recovery(self):
        result, p_run, p_lock, p_sweep = self._run([MagicMock(returncode=0)])
        self.assertTrue(result)
        self.assertEqual(p_run.call_count, 1)
        p_lock.assert_not_called()
        p_sweep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
