"""`duration_min` is authoritative: a slow blue cycle cannot stretch the event.

caught live (run-59a685c0): a 25-minute event reached capture at minute 50
because `supervise_workers` joined an in-flight opencode cycle with a budget of a whole
CYCLE_TIMEOUT. The join is now capped by what is left of the window plus a bounded grace, and
the cycle itself is aborted once the event window closes, so capture and teardown always run
on time.
"""

import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scrim import blue_agent, endpoints, event_run, scoreboard_monitor, supervisor


class JoinIsCappedByTheEventWindow(unittest.TestCase):

    def test_a_worker_still_in_a_cycle_does_not_hold_the_harness_past_the_deadline(self):
        stuck = threading.Thread(target=lambda: time.sleep(2), name="blue-feed-1", daemon=True)
        stuck.start()
        stop = threading.Event()
        with mock.patch.object(supervisor, "WORKER_JOIN_BUDGET", 30), \
                mock.patch.object(supervisor, "EVENT_END_GRACE", 0.2):
            started = time.time()
            reported = supervisor.supervise_workers([stuck], time.time(), stop, poll=0.01)
            elapsed = time.time() - started
        # pre-fix this waited out WORKER_JOIN_BUDGET (a full cycle) and reported nothing
        self.assertLess(elapsed, 5)
        self.assertEqual([t.name for t in reported], ["blue-feed-1"])
        self.assertTrue(stop.is_set())

    def test_a_worker_wrapping_up_inside_the_grace_is_not_reported(self):
        stop = threading.Event()
        t = threading.Thread(target=lambda: stop.wait(5), name="blue-feed-1")
        t.start()
        with mock.patch.object(supervisor, "EVENT_END_GRACE", 1.0):
            reported = supervisor.supervise_workers([t], time.time(), stop, poll=0.01)
        self.assertEqual(reported, [])


class InFlightCycleIsAbortedAtTheDeadline(unittest.TestCase):

    def _slow_popen(self, real):
        def slow(cmd, **kw):
            kw.pop("stdin", None)
            return real(["bash", "-c", "sleep 60"], stdin=subprocess.DEVNULL, **kw)
        return slow

    def test_stop_kills_the_cycle_instead_of_waiting_out_its_budget(self):
        real = subprocess.Popen
        wd = Path(tempfile.mkdtemp()) / "blue-team1"
        wd.mkdir(parents=True)
        args = SimpleNamespace(blue_base_url="http://llm.invalid/v1", blue_model="m")
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        with mock.patch.object(blue_agent.subprocess, "Popen", side_effect=self._slow_popen(real)):
            started = time.time()
            with self.assertRaises(subprocess.TimeoutExpired):
                blue_agent._opencode_run(args, "prompt", wd, {}, stop=stop)
            elapsed = time.time() - started
        self.assertLess(elapsed, 10, "the cycle ran out its own budget instead of the deadline")
        self.assertTrue((wd / "cycles" / "last-timeout.partial.log").exists())

    def test_a_cycle_without_stop_still_runs_to_completion(self):
        real = subprocess.Popen
        wd = Path(tempfile.mkdtemp()) / "blue-team1"
        wd.mkdir(parents=True)
        args = SimpleNamespace(blue_base_url="http://llm.invalid/v1", blue_model="m")

        def quick(cmd, **kw):
            kw.pop("stdin", None)
            return real(["bash", "-c", "echo done"], stdin=subprocess.DEVNULL, **kw)

        with mock.patch.object(blue_agent.subprocess, "Popen", side_effect=quick):
            r = blue_agent._opencode_run(args, "prompt", wd, {})
        self.assertEqual(r.returncode, 0)
        self.assertIn("done", r.stdout)


class StageRunEndsAtTheDeadline(unittest.TestCase):

    def test_the_event_returns_at_duration_min_with_a_cycle_in_flight(self):
        args = SimpleNamespace(duration_min=0.02, teams=1, blue_watchdog=False)
        stop_holders = []

        def feed(n, a, creds, t0, stop, lock, first_delay=0.0):
            stop_holders.append(stop)
            time.sleep(2)                     # parked in a cycle, ignoring stop

        with mock.patch.object(blue_agent, "blue_feed_loop", feed), \
                mock.patch.object(scoreboard_monitor, "monitor_loop", lambda *a: None), \
                mock.patch.object(endpoints, "blue_ep", lambda *a: ("http://llm.invalid/v1", "m")), \
                mock.patch.object(supervisor, "EVENT_END_GRACE", 0.2):
            t0 = time.time()
            with self.assertRaises(supervisor.WorkerDiedError) as raised:
                event_run.stage_run(args, {}, t0)
            elapsed = time.time() - t0
        self.assertLess(elapsed, 5)
        self.assertIn("blue-feed-1", str(raised.exception))
        self.assertTrue(stop_holders and stop_holders[0].is_set())


if __name__ == "__main__":
    unittest.main()
