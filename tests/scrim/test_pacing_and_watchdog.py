"""A5 — pacing profiles per competition + mid-event red stall auto-recovery.

run-bd70a985 judged a 3-box range against the 4-down cap of a 7-box estate (an
invented FAIL), and the red loop stalled twice with nobody noticing until the
report. red.pacing.* Compfile lines now feed the manifest the report reads, and a
watchdog thread restarts a dead/hung tlaloc loop without disturbing the event clock."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from scrim import red_watchdog  # noqa: E402
from scrim.red_stage import red_pacing_caps  # noqa: E402
from scrim import run_manifest  # noqa: E402


class PacingProfile(unittest.TestCase):
    def test_defaults_when_no_comp(self):
        self.assertEqual(red_pacing_caps()["max_concurrent_down_end"], 4)

    def test_compfile_profile_overrides_and_leaves_rest(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "Compfile").write_text(
                "# small range\nred.pacing.end 3\nred.pacing.endgame 4\n")
            caps = red_pacing_caps(td)
            self.assertEqual(caps["max_concurrent_down_end"], 3)
            self.assertEqual(caps["max_concurrent_down_endgame"], 4)
            self.assertEqual(caps["max_concurrent_down_start"], 2)   # untouched

    def test_junk_value_keeps_the_default(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "Compfile").write_text("red.pacing.end lots\n")
            self.assertEqual(red_pacing_caps(td)["max_concurrent_down_end"], 4)

    def test_manifest_records_the_profiled_caps(self):
        with tempfile.TemporaryDirectory() as td:
            comp = Path(td) / "splunk-scenario"
            comp.mkdir()
            (comp / "Compfile").write_text("red.pacing.end 3\n")
            args = SimpleNamespace(competition="splunk-scenario", teams=4,
                                   duration_min=120, keep_range=False,
                                   keep_templates=False, blue_watchdog=False)
            run = Path(td) / "run"; run.mkdir()
            with mock.patch("common.comp_paths.comps_root", return_value=td):
                run_manifest.record_phase(run, args, "event", t0=1)
            manifest = json.loads((run / "run.json").read_text())
            self.assertEqual(manifest["pacing"]["max_concurrent_down_end"], 3)


class StallWatchdog(unittest.TestCase):
    def _proc(self, stdout, rc=0):
        return SimpleNamespace(stdout=stdout, returncode=rc)

    def _tick(self, probe_stdout, restart_rc=0):
        calls = []

        def fake(cmd, timeout=None, check=True):
            joined = " ".join(cmd)
            calls.append(joined)
            return self._proc("active\n" if "restart" in joined else probe_stdout)
        with mock.patch("scrim.red_link._red_ssh_ctx",
                        return_value=("root@10.0.0.9", ["-i", "/k"], None)), \
             mock.patch("scrim.red_link.procs.run_tree", side_effect=fake):
            return red_watchdog.check_once(SimpleNamespace()), calls

    def test_active_and_fresh_leaves_red_alone(self):
        recovered, calls = self._tick("active\n120\n")
        self.assertFalse(recovered)
        self.assertEqual(len(calls), 1)          # probe only — no restart

    def test_inactive_unit_is_restarted(self):
        recovered, calls = self._tick("inactive\n5000\n")
        self.assertTrue(recovered)
        self.assertIn("systemctl restart tlaloc.service", calls[1])

    def test_active_but_hung_state_file_is_restarted(self):
        recovered, calls = self._tick("active\n1800\n")
        self.assertTrue(recovered)
        self.assertIn("restart", calls[1])

    def test_restart_keeps_the_event_clock(self):
        # no world.json event_start rewrite — a mid-event restart must not reset the timer
        _, calls = self._tick("inactive\n5000\n")
        self.assertNotIn("event_start", calls[1])

    def test_unreadable_probe_is_not_an_emergency(self):
        recovered, calls = self._tick("")
        self.assertFalse(recovered)
        self.assertEqual(len(calls), 1)

    def test_recoveries_land_in_the_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run"; run.mkdir()
            run_manifest.note_red_watchdog_restart(run)
            run_manifest.note_red_watchdog_restart(run)
            manifest = json.loads((run / "run.json").read_text())
            self.assertEqual(len(manifest["red_watchdog_restarts"]), 2)


class WatchdogThreadIsLiveButUnsupervised(unittest.TestCase):
    """stage_run must actually START the watchdog (the A5 unit tests call watch_loop
    directly — a dead import is all the coverage they'd tolerate), and must NOT put it
    in the supervised list (a watchdog that dies degrades protection, not the event)."""

    def test_stage_run_starts_watch_loop_outside_the_supervised_set(self):
        import threading as th
        import time as tm
        from scrim import blue_agent, endpoints, event_run, scoreboard_monitor, supervisor

        started = th.Event()

        def fake_watch(args, stop):
            started.set()          # then leave immediately: a dead watchdog

        def live_feed(n, a, creds, t0, stop, lock, first_delay=0.0):
            stop.wait(5)           # supervised workers must live until the deadline

        args = SimpleNamespace(duration_min=0.02, teams=1, blue_watchdog=False,
                               run_dir="/tmp")
        with mock.patch.object(red_watchdog, "watch_loop", fake_watch), \
                mock.patch.object(blue_agent, "blue_feed_loop", live_feed), \
                mock.patch.object(scoreboard_monitor, "monitor_loop",
                                  lambda *a: a[3].wait(5)), \
                mock.patch.object(endpoints, "blue_ep", lambda *a: ("http://llm.invalid/v1", "m")), \
                mock.patch.object(supervisor, "EVENT_END_GRACE", 0.2):
            try:
                event_run.stage_run(args, {}, tm.time())   # pre-fix: watch_loop never ran
            except supervisor.WorkerDiedError as e:
                # whatever the supervisor says about the stubs, the UNSUPERVISED
                # watchdog may never appear — its death is protection, not an event
                self.assertNotIn("red-watchdog", str(e))
        self.assertTrue(started.is_set(), "stage_run never started the red watchdog")


if __name__ == "__main__":
    unittest.main()
