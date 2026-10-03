"""Regression tests for the red-vs-blue scrim harness defect audit (findings D1-D12).

Every test here pins a defect that was found by reading the code and that only shows up
on live infrastructure (a 90-minute run against real ranges), where the evidence is a
post-hoc report — so the failure paths matter more than the happy path. Each fix cites
its incident in the source; each test below names the mechanism it protects.

Offline and fast: no network, no SSH, no real opencode. The one slow fixture is D1's
orphaned-grandchild reproduction, which is two sub-second timeouts.
"""

import contextlib
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    # run-agent-scrim.py imports utils/config_ops at module scope; the harness tests in
    # this repo load modules that rely on the repo root being importable.
    sys.path.insert(0, str(_REPO))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _REPO / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scrim = _load("run_agent_scrim_defects", "run-agent-scrim.py")
report = _load("scrim_report_defects", "scrim-report.py")
sched = _load("run_schedule_defects", "run-schedule.py")
beacon = _load("beacon_ops_defects", "beacon_ops.py")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.05)
    return not _pid_alive(pid)


class _suppress:
    """Tiny context manager so a fixture cleanup cannot mask the real assertion."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


# D1 ---------------------------------------------------------------------------
class ProcessTreeSupervision(unittest.TestCase):
    """run_tree must reap the whole process group; subprocess.run(timeout=) does not."""

    # A grandchild that IGNORES SIGINT/SIGTERM: only the SIGKILL escalation can reap it,
    # which is exactly the terraform/nakon/badauto case (they keep mutating infra and
    # holding the deploy lock after the direct child is killed).
    @staticmethod
    def _orphan_script(pidfile):
        return (f"bash -c 'trap \"\" INT TERM; exec sleep 300' & "
                f"echo $! > {pidfile}; sleep 300")

    def test_plain_subprocess_run_orphans_the_grandchild(self):
        """The defect being fixed, demonstrated against the stdlib."""
        tmp = Path(tempfile.mkdtemp())
        pidfile = tmp / "gc.pid"
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run(["bash", "-c", self._orphan_script(pidfile)],
                           timeout=0.6, capture_output=True)
        pid = int(pidfile.read_text())
        try:
            self.assertTrue(_pid_alive(pid),
                            "fixture broken: the grandchild should have outlived subprocess.run")
        finally:
            with _suppress():
                os.kill(pid, signal.SIGKILL)

    def test_run_tree_kills_the_whole_process_group(self):
        tmp = Path(tempfile.mkdtemp())
        pidfile = tmp / "gc.pid"
        with self.assertRaises(scrim.ScrimTimeout):
            scrim.run_tree(["bash", "-c", self._orphan_script(pidfile)],
                           timeout=0.6, grace=0.4, check=False)
        pid = int(pidfile.read_text())
        if not _wait_dead(pid):
            os.kill(pid, signal.SIGKILL)
            self.fail(f"grandchild pid {pid} survived the run_tree timeout")
        self.assertFalse(_pid_alive(pid))

    def test_timeout_error_is_typed_and_names_the_resume_path(self):
        with self.assertRaises(scrim.ScrimTimeout) as ctx:
            scrim.run_tree([sys.executable, "-c", "import time; time.sleep(30)"],
                           timeout=0.3, grace=0.2, check=False)
        self.assertIsInstance(ctx.exception, subprocess.TimeoutExpired)
        msg = str(ctx.exception)
        self.assertIn("process group", msg)
        self.assertIn("--skip-deploy", msg)
        self.assertIn("--resume-event", msg)

    def test_run_tree_still_returns_a_completed_process(self):
        r = scrim.run_tree([sys.executable, "-c", "print('hi')"], timeout=30, check=False)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "hi")
        with self.assertRaises(RuntimeError):
            scrim.run_tree([sys.executable, "-c", "raise SystemExit(3)"], timeout=30)

    def test_stdin_is_delivered_without_argv_exposure(self):
        r = scrim.run_tree(["bash", "-c", "cat"], timeout=30, check=False,
                           stdin_text="secret\nscript\n")
        self.assertEqual(r.stdout, "secret\nscript\n")


# D2 ---------------------------------------------------------------------------
class TrackingLock:
    """Records the exact acquire/release order (a plain Lock cannot show the interval)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.events = []

    def __enter__(self):
        self.events.append("acquire")
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        self.events.append("release")
        return False


class BlueFeedLockAndTimeout(unittest.TestCase):
    def _fake_opencode(self, results, calls, stop=None, stop_on=()):
        def fake(args, prompt, wd, env):
            calls.append(1)
            n = len(calls)
            if stop is not None and n in stop_on:
                stop.set()
            result = results[min(n, len(results)) - 1]
            if isinstance(result, Exception):
                raise result
            return result
        return fake

    def test_lock_is_released_between_the_attempt_and_its_retry(self):
        calls = []
        fake = self._fake_opencode(
            [subprocess.CompletedProcess([], 1, "", ""), subprocess.CompletedProcess([], 0, "", "")],
            calls)
        lock = TrackingLock()
        with mock.patch.object(scrim, "_opencode_run", fake):
            r = scrim.run_cycle_with_retry(SimpleNamespace(), "p", "/tmp/blue-team1", {},
                                           lock, threading.Event(), team=1)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(calls), 2)
        # pre-fix this was one acquisition around BOTH attempts ("acquire", "release"):
        # two hung attempts held the shared endpoint lock for up to 2x CYCLE_TIMEOUT.
        self.assertEqual(lock.events, ["acquire", "release", "acquire", "release"])
        self.assertFalse(lock._lock.locked())

    def test_retry_is_skipped_once_stop_is_set(self):
        calls = []
        fake = self._fake_opencode([subprocess.CompletedProcess([], 1, "", "")], calls)
        stop = threading.Event()
        stop.set()
        with mock.patch.object(scrim, "_opencode_run", fake):
            r = scrim.run_cycle_with_retry(SimpleNamespace(), "p", "/tmp/blue-team1", {},
                                           threading.Lock(), stop, team=1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(r.returncode, 1)

    def test_timeout_on_the_retry_still_writes_the_header_the_report_counts(self):
        rd = Path(tempfile.mkdtemp())
        calls = []
        stop = threading.Event()
        # first attempt fails with rc!=0 (so the retry happens), the RETRY times out and
        # sets the stop event so the loop exits instead of waiting out CYCLE_TARGET_PERIOD.
        fake = self._fake_opencode(
            [subprocess.CompletedProcess([], 1, "boom", ""),
             subprocess.TimeoutExpired("opencode", 1800)],
            calls, stop=stop, stop_on=(2,))
        args = SimpleNamespace(run_dir=str(rd), duration_min=90, teams=1,
                               blue_base_url="http://llm.invalid/v1", blue_model="m")
        creds = {"TEAM1_ID": "101", "RUN_DIR": str(rd)}
        with mock.patch.object(scrim, "_opencode_run", fake), \
                mock.patch.object(scrim, "blue_cycle_prompt", lambda *a, **k: "prompt"), \
                mock.patch.object(scrim, "scoreboard_delta", lambda *a, **k: ""), \
                mock.patch.object(scrim, "inject_brief", lambda *a, **k: ""), \
                mock.patch.object(scrim, "api_key", lambda *a, **k: "k"):
            scrim.blue_feed_loop(1, args, creds, time.time(), stop, threading.Lock(),
                                 first_delay=0)
        self.assertEqual(len(calls), 2, "the retry should have been attempted")
        feed = (rd / "blue-team1" / "feed.log").read_text()
        self.assertIn("===== cycle T+0 TIMEOUT =====", feed)
        # ...and the rehearsal gate reads exactly this header. Pre-fix, a retried-then-
        # timed-out cycle was the one timeout the report could not see.
        self.assertEqual(report.blue_metrics(rd)["timeouts"], 1)

    def test_write_cycle_timeout_header_is_a_single_source_of_truth(self):
        rd = Path(tempfile.mkdtemp())
        scrim.write_cycle_timeout(rd, 42)
        self.assertIn("===== cycle T+42 TIMEOUT =====", (rd / "feed.log").read_text())


# D3 ---------------------------------------------------------------------------
class WorkerSupervision(unittest.TestCase):
    def test_dead_workers_reports_each_thread_once(self):
        t = threading.Thread(target=lambda: None, name="feed"); t.start(); t.join()
        reported = []
        self.assertEqual(scrim.dead_workers([t], reported), [t])
        reported.append(t)
        self.assertEqual(scrim.dead_workers([t], reported), [])

    def test_join_workers_uses_one_shared_deadline_and_returns_stragglers(self):
        quick = threading.Thread(target=lambda: None, name="quick"); quick.start()
        stuck = threading.Thread(target=lambda: time.sleep(10), name="stuck", daemon=True)
        stuck.start()
        alive = scrim.join_workers([quick, stuck], budget=0.2)
        self.assertEqual(alive, [stuck])
        self.assertFalse(quick.is_alive())

    def test_join_budget_covers_a_full_cycle(self):
        # the old join(timeout=10) abandoned a live 1800s opencode cycle and capture /
        # teardown then wrote into (and destroyed infra under) the same workdir.
        self.assertGreaterEqual(scrim.WORKER_JOIN_BUDGET, scrim.CYCLE_TIMEOUT)

    def test_supervise_workers_reports_a_worker_that_died(self):
        t = threading.Thread(target=lambda: None, name="blue-feed-1"); t.start()
        stop = threading.Event()
        reported = scrim.supervise_workers([t], time.time() + 0.05, stop, poll=0.01)
        self.assertEqual([x.name for x in reported], ["blue-feed-1"])
        self.assertTrue(stop.is_set())

    def test_supervise_workers_reports_a_worker_that_refuses_to_stop(self):
        stuck = threading.Thread(target=lambda: time.sleep(10), name="monitor", daemon=True)
        stuck.start()
        stop = threading.Event()
        with mock.patch.object(scrim, "WORKER_JOIN_BUDGET", 0.05):
            reported = scrim.supervise_workers([stuck], time.time() - 1, stop, poll=0.01)
        self.assertEqual([x.name for x in reported], ["monitor"])
        self.assertTrue(stop.is_set())

    def test_team_tid_raises_a_clear_error_on_an_error_body(self):
        # an error/HTML body used to make next(...) raise StopIteration/TypeError inside
        # the monitor thread; that thread then died with nothing watching it.
        for body in ('{"error":"Forbidden"}', "<html>500</html>", '{"ID":1}'):
            with mock.patch.object(scrim, "qget",
                                   lambda *a, **k: SimpleNamespace(stdout=body)):
                with self.assertRaises(ValueError):
                    scrim._team_tid({"ENGINE_IP": "x"}, "team1", "/tmp/j.jar", "team1")

    def test_team_tid_reports_an_unregistered_team(self):
        with mock.patch.object(scrim, "qget",
                               lambda *a, **k: SimpleNamespace(stdout='[{"ID":1,"Name":"team2"}]')):
            with self.assertRaises(ValueError):
                scrim._team_tid({"ENGINE_IP": "x"}, "team1", "/tmp/j.jar", "team1")

    def test_worker_death_still_captures_and_tears_down(self):
        calls = []
        with mock.patch.object(scrim, "stage_run",
                               side_effect=scrim.WorkerDiedError("blue-feed-1 died")), \
                mock.patch.object(scrim, "stage_capture", lambda *a: calls.append("capture")), \
                mock.patch.object(scrim, "stage_teardown", lambda *a: calls.append("teardown")):
            failure = scrim.run_event_and_finish(SimpleNamespace(), {}, 0.0)
        self.assertEqual(calls, ["capture", "teardown"])
        self.assertIn("blue-feed-1", failure)


# D4 ---------------------------------------------------------------------------
class RedLlmWatchdog(unittest.TestCase):
    class Tunnel:
        def __init__(self):
            self.restarts = 0
            self.remote_port = 8180

        def restart(self):
            self.restarts += 1

    def setUp(self):
        scrim._llm_watch.clear()

    def _args(self, rd, url="http://llm.local/v1", tunnel=None):
        return SimpleNamespace(run_dir=str(rd), llm_base_url=url, red_tunnel=tunnel or "off")

    def test_hysteresis_restarts_the_tunnel_once_per_episode(self):
        rd = Path(tempfile.mkdtemp())
        tunnel = self.Tunnel()
        args = self._args(rd, tunnel=tunnel)
        with mock.patch.object(scrim, "check_red_llm", lambda *a: False):
            for tick in range(5):
                scrim.red_llm_watch(args, tick * 5)
        # 5 failed probes -> one rescue, not five
        self.assertEqual(tunnel.restarts, 1)
        lines = scrim.alerts_path(rd).read_text().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["kind"], "red_llm_restart")
        self.assertEqual(rec["failed_probes"], scrim.RED_LLM_FAIL_THRESHOLD)
        self.assertEqual(stat.S_IMODE(scrim.alerts_path(rd).stat().st_mode), 0o600)

    def test_a_single_blip_alerts_but_does_not_restart(self):
        rd = Path(tempfile.mkdtemp())
        tunnel = self.Tunnel()
        args = self._args(rd, tunnel=tunnel)
        with mock.patch.object(scrim, "check_red_llm", lambda *a: False):
            scrim.red_llm_watch(args, 0)
        self.assertEqual(tunnel.restarts, 0)
        self.assertFalse(scrim.alerts_path(rd).exists())

    def test_recovery_clears_the_episode_so_a_later_outage_is_rescued(self):
        rd = Path(tempfile.mkdtemp())
        tunnel = self.Tunnel()
        args = self._args(rd, tunnel=tunnel)
        reachable = {"v": False}
        with mock.patch.object(scrim, "check_red_llm", lambda *a: reachable["v"]):
            for tick in range(3):
                scrim.red_llm_watch(args, tick)
            self.assertEqual(tunnel.restarts, 1)
            reachable["v"] = True
            scrim.red_llm_watch(args, 3)
            self.assertIsNone(scrim._llm_watch[args.llm_base_url]["since"])
            reachable["v"] = False
            for tick in range(4, 7):
                scrim.red_llm_watch(args, tick)
        self.assertEqual(tunnel.restarts, 2)

    def test_state_is_per_endpoint(self):
        rd = Path(tempfile.mkdtemp())
        a = self._args(rd, url="http://a.invalid/v1")
        b = self._args(rd, url="http://b.invalid/v1")
        with mock.patch.object(scrim, "check_red_llm", lambda *a_: False):
            scrim.red_llm_watch(a, 0)
            scrim.red_llm_watch(b, 0)
        self.assertEqual(scrim._llm_watch["http://a.invalid/v1"]["failures"], 1)
        self.assertEqual(scrim._llm_watch["http://b.invalid/v1"]["failures"], 1)

    def test_report_survives_red_events_without_a_world_clock(self):
        # A run dir with red events but no world.json has no event_start; the stall maths
        # used to compare None to a float and abort the WHOLE report (so a timeout the
        # rehearsal gate must see was never counted).
        run = Path(tempfile.mkdtemp())
        (run / "evidence" / "red").mkdir(parents=True)
        (run / "evidence" / "red" / "events.jsonl").write_text(json.dumps(
            {"ts": "2026-09-29T07:00:00Z", "kind": "action", "ok": True,
             "tactic": "impact_service", "target": "192.168.101.4", "detail": "x"}) + "\n")
        out, meta = report.build_report(run)
        self.assertIn("## Gates", out)
        self.assertEqual(meta["gates_failed"], meta["gates_failed"])  # built, did not raise

    def test_report_surfaces_the_alert_journal(self):
        rd = Path(tempfile.mkdtemp())
        ev = rd / "evidence"
        ev.mkdir()
        (ev / scrim.ALERTS_FILENAME).write_text(json.dumps(
            {"ts": "2026-09-29T07:00:00", "kind": "red_llm_restart",
             "detail": "red01 missed 3 consecutive LLM probes"}) + "\n")
        out, _meta = report.build_report(rd)
        self.assertIn("## Run alerts", out)
        self.assertIn("red_llm_restart", out)
        self.assertEqual(len(report.load_alerts(rd)), 1)


# D5/D6 ------------------------------------------------------------------------
class EvidenceHygiene(unittest.TestCase):
    def test_jar_path_is_private_and_out_of_shared_tmp(self):
        rd = Path(tempfile.mkdtemp())
        p = Path(scrim.jar_path(rd, "team1"))
        self.assertTrue(p.is_file())
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)
        self.assertTrue(str(p).startswith(str(rd)))
        self.assertFalse(str(p).startswith("/tmp/jar."))
        # deterministic basename: the generated qlogin/score.py helpers and the driver
        # must agree on one file across processes.
        self.assertEqual(scrim.jar_path(rd, "team1"), str(p))
        # an existing jar is never truncated by looking it up again
        p.write_text("cookie")
        scrim.jar_path(rd, "team1")
        self.assertEqual(p.read_text(), "cookie")

    def test_no_predictable_tmp_jar_path_in_the_driver_or_its_generated_helpers(self):
        src = (_REPO / "run-agent-scrim.py").read_text()
        # no string literal builds the old shared-/tmp path...
        self.assertNotIn('"/tmp/jar.', src)
        # ...and scrim.env points the blue agent's helpers at the run-dir jar instead
        self.assertIn("JAR={jar_path(run_dir, f'team{n}')}", src)

    def test_evidence_writer_is_atomic_and_0600(self):
        rd = Path(tempfile.mkdtemp())
        out = rd / "final-scoreboard.json"
        scrim.write_evidence(out, '{"a": 1}')
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual(json.loads(out.read_text()), {"a": 1})
        self.assertFalse((rd / "final-scoreboard.json.tmp").exists())

    def test_secure_evidence_tightens_a_loose_copy(self):
        out = Path(tempfile.mkdtemp()) / "feed.log"
        out.write_text("x")
        out.chmod(0o644)
        scrim.secure_evidence(out)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)

    def test_startup_sets_a_restrictive_umask(self):
        for name in ("run-agent-scrim.py", "run-schedule.py"):
            self.assertIn("os.umask(0o077)", (_REPO / name).read_text(), name)

    def test_no_dangling_open_write_leaks(self):
        # `.open("a").write(...)` leaks one descriptor per call (monitor.log every
        # MONITOR_INTERVAL, feed.log every cycle) for the length of the event.
        self.assertNotIn('.open("a").write(', (_REPO / "run-agent-scrim.py").read_text())

    def test_json_error_tests_the_key_not_the_text(self):
        self.assertTrue(scrim.json_error('{"error":"Forbidden"}'))
        self.assertTrue(scrim.json_error('{"error": "x"}'))
        self.assertFalse(scrim.json_error('{"service":"error-handler","up":true}'))
        self.assertFalse(scrim.json_error('[{"error":"x"}]'))
        self.assertFalse(scrim.json_error('"error"'))
        self.assertFalse(scrim.json_error(""))
        self.assertFalse(scrim.json_error("garbage"))
        self.assertFalse(scrim.json_error('{"outer":{"error":1}}'))

    def test_qget_relogins_only_for_a_real_error_object(self):
        for body, expected_logins in (('{"service":"error-handler","up":true}', 0),
                                      ('[{"ID":1,"Name":"team1"}]', 0),
                                      ('{"error":"Forbidden"}', 1)):
            logins = []
            with mock.patch.object(scrim, "_qlogin", lambda *a: logins.append(1)), \
                    mock.patch.object(scrim.subprocess, "run",
                                      lambda *a, **k: SimpleNamespace(returncode=0, stdout=body)):
                again = scrim.qget({"ENGINE_IP": "192.0.2.1"}, "team1",
                                   "/tmp/does-not-matter.jar", "/api/teams")
            self.assertEqual(len(logins), expected_logins, body)
            self.assertIsNotNone(again)

    def test_login_writes_via_temp_then_rename_and_keeps_the_old_jar_on_failure(self):
        rd = Path(tempfile.mkdtemp())
        jar = Path(scrim.jar_path(rd, "team1"))
        jar.write_text("old-cookie")
        creds = {"ENGINE_IP": "192.0.2.1", "TEAM1_PW": "pw"}
        # curl failing (e.g. engine unreachable) must not destroy a working session
        with mock.patch.object(scrim.subprocess, "run",
                               lambda *a, **k: SimpleNamespace(returncode=7)):
            self.assertFalse(scrim._qlogin(creds, "team1", str(jar)))
        self.assertEqual(jar.read_text(), "old-cookie")

        def fake_curl(cmd, *a, **k):
            Path(cmd[cmd.index("-c") + 1]).write_text("new-cookie")
            return SimpleNamespace(returncode=0)

        with mock.patch.object(scrim.subprocess, "run", fake_curl):
            self.assertTrue(scrim._qlogin(creds, "team1", str(jar)))
        self.assertEqual(jar.read_text(), "new-cookie")
        self.assertEqual(stat.S_IMODE(jar.stat().st_mode), 0o600)
        # the temp file was renamed onto the jar, not left behind
        self.assertEqual(sorted(p.name for p in (rd / ".jars").iterdir()), ["team1.jar"])

    def test_jar_lock_is_per_account_and_reentrant(self):
        self.assertIs(scrim.jar_lock("team1"), scrim.jar_lock("team1"))
        self.assertIsNot(scrim.jar_lock("team2"), scrim.jar_lock("team1"))
        with scrim.jar_lock("team1"):
            with scrim.jar_lock("team1"):
                pass  # RLock: qget -> _qlogin must not self-deadlock


# D7 ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, payload=None, status=200, raises=None):
        self._payload = payload
        self.status_code = status
        self._raises = raises

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


class FakeSession:
    """Minimal requests.Session stand-in for dump_scoreboard/end_event."""

    def __init__(self, teams=None, raises=None, post_status=200, post_raises=None):
        self._teams = [{"ID": 1, "Name": "team1"}] if teams is None else teams
        self._raises = raises
        self._post_status = post_status
        self._post_raises = post_raises
        self.posts = []

    def get(self, url, timeout=None):
        if self._raises is not None:
            raise self._raises
        if url.endswith("/api/teams"):
            return FakeResp(self._teams)
        if url.endswith("/api/injects"):
            return FakeResp([])
        return FakeResp([{"ServiceName": "web01", "Last10Rounds": []}])

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        if self._post_raises is not None:
            raise self._post_raises
        return FakeResp({"ok": True}, status=self._post_status)


class ScheduleEndAlwaysPauses(unittest.TestCase):
    def test_dump_survives_an_engine_500(self):
        import requests
        comp = Path(tempfile.mkdtemp())
        session = FakeSession(raises=requests.RequestException("500 Server Error"))
        errors = sched.dump_scoreboard("http://192.0.2.1", session, comp)
        self.assertTrue(errors)
        dumps = sorted((comp / "evidence").glob("event-final-*.json"))
        self.assertEqual(len(dumps), 1)
        self.assertEqual(stat.S_IMODE(dumps[0].stat().st_mode), 0o600)
        self.assertIn("teams_error", json.loads(dumps[0].read_text()))

    def test_dump_survives_a_non_json_body(self):
        comp = Path(tempfile.mkdtemp())
        session = FakeSession(raises=ValueError("not JSON"))
        errors = sched.dump_scoreboard("http://192.0.2.1", session, comp)
        self.assertTrue(errors)
        self.assertTrue(list((comp / "evidence").glob("event-final-*.json")))

    def test_end_pauses_even_when_the_capture_fails(self):
        import requests
        comp = Path(tempfile.mkdtemp())
        session = FakeSession(raises=requests.RequestException("500 Server Error"))
        rc = sched.end_event("http://192.0.2.1", session, comp)
        self.assertEqual(len(session.posts), 1, "the pause must still be attempted")
        self.assertIn("/api/engine/pause", session.posts[0][0])
        self.assertEqual(session.posts[0][1], {"pause": True})
        self.assertEqual(rc, 1)

    def test_end_pauses_even_when_the_dump_blows_up(self):
        comp = Path(tempfile.mkdtemp())
        session = FakeSession()
        with mock.patch.object(sched, "dump_scoreboard", side_effect=RuntimeError("boom")):
            rc = sched.end_event("http://192.0.2.1", session, comp)
        self.assertEqual(len(session.posts), 1)
        self.assertEqual(rc, 1)

    def test_end_reports_a_failed_pause_with_a_nonzero_exit(self):
        comp = Path(tempfile.mkdtemp())
        session = FakeSession(post_status=500)
        self.assertEqual(sched.end_event("http://192.0.2.1", session, comp), 1)

    def test_end_reports_a_dropped_pause_connection(self):
        import requests
        comp = Path(tempfile.mkdtemp())
        session = FakeSession(post_raises=requests.ConnectionError("refused"))
        self.assertEqual(sched.end_event("http://192.0.2.1", session, comp), 1)

    def test_end_is_zero_only_when_both_steps_succeed(self):
        comp = Path(tempfile.mkdtemp())
        session = FakeSession()
        self.assertEqual(sched.end_event("http://192.0.2.1", session, comp), 0)
        self.assertEqual(len(session.posts), 1)


# D8 ---------------------------------------------------------------------------
class LineupDerivation(unittest.TestCase):
    AMONGUS = _REPO / "competitions" / "amongus-cde"
    LEGACY_17B = {"web01": 45, "app01": 60, "db01": 90}

    def _boxes(self, comp):
        return json.loads((_REPO / "competitions" / comp / "boxes.json").read_text())

    def test_box_labels_come_from_the_competition_boxes_json(self):
        labels = report.box_labels(self.AMONGUS)
        self.assertEqual(labels, {"2": "mira", "3": "skeld", "4": "airship", "5": "polus"})
        self.assertEqual(report.host_label("192.168.101.4", labels), "1:airship")
        # a non-team address (red01) keeps its raw octets and is never team-labelled
        self.assertEqual(report.host_label("10.0.0.198", labels), ".198")
        self.assertNotIn(":", report.host_label("10.0.0.198", labels))

    def test_box_labels_resolve_a_suffixed_run_dir(self):
        fake_run = Path(tempfile.mkdtemp()) / "amongus-cde-2026-run2"
        fake_run.mkdir()
        self.assertEqual(report.box_labels(fake_run)["4"], "airship")

    def test_unknown_run_dir_does_not_invent_a_name_from_the_wrong_lineup(self):
        fake_run = Path(tempfile.mkdtemp()) / "totally-unknown-run"
        fake_run.mkdir()
        labels = report.box_labels(fake_run)
        self.assertEqual(labels, {})
        # empty map -> raw dotted octet, NOT the 17b dc01/win01/web01 guess
        self.assertEqual(report.host_label("192.168.101.4", labels), "1:.4")

    def test_report_says_so_when_it_cannot_resolve_the_lineup(self):
        run = Path(tempfile.mkdtemp()) / "mystery-run"
        (run / "evidence" / "red").mkdir(parents=True)
        (run / "evidence" / "red" / "events.jsonl").write_text(json.dumps(
            {"ts": "2026-09-29T07:00:00Z", "kind": "action", "ok": True,
             "tactic": "impact_service", "target": "192.168.101.4",
             "data": {"unit": "httpd"}, "detail": "httpd is DOWN on 192.168.101.4"}) + "\n")
        out, _meta = report.build_report(run)
        self.assertIn("no boxes.json could be resolved", out)
        self.assertIn("1:.4", out)

    def test_legacy_fallback_still_serves_unattributed_callers(self):
        self.assertTrue(report.host_label("192.168.101.4").startswith("1:"))

    def test_red_timeline_labels_the_amongus_lineup(self):
        run = Path(tempfile.mkdtemp()) / "amongus-cde-2026-run2"
        (run / "evidence" / "red").mkdir(parents=True)
        ev = {"ts": "2026-09-29T07:00:00Z", "kind": "action", "ok": True,
              "tactic": "impact_service", "target": "192.168.101.4",
              "data": {"unit": "httpd"}, "detail": "httpd is DOWN on 192.168.101.4"}
        (run / "evidence" / "red" / "events.jsonl").write_text(json.dumps(ev) + "\n")
        t0 = report.parse_ts(ev["ts"])
        rm = report.red_metrics([ev], t0, {}, report.box_labels(run))
        self.assertEqual(rm["timeline"][0][1], "1:airship")
        out, _meta = report.build_report(run)
        self.assertIn("1:airship", out)
        self.assertNotIn("1:web01", out)

    def test_beacon_plan_matches_the_17b_table_it_replaces(self):
        plan = dict(beacon.beacon_plan(self._boxes("agent-scrim")))
        self.assertEqual(plan, self.LEGACY_17B)

    def test_beacon_plan_covers_a_non_17b_lineup(self):
        boxes = self._boxes("amongus-cde")
        old_gate = [b["name"] for b in boxes
                    if not beacon._is_windows(b["template"])
                    and b["name"] in self.LEGACY_17B]           # the pre-fix gate
        plan = beacon.beacon_plan(boxes)
        self.assertEqual(old_gate, [], "fixture: the old name gate plants nothing here")
        self.assertEqual([name for name, _interval in plan], ["airship", "polus"])
        self.assertEqual([i for _name, i in plan], [45, 60])

    def test_beacon_plan_skips_windows_and_unmanaged_appliances(self):
        # loadtest-cr-a's lineup (the comp dir was pruned 2026-10-02): two Windows
        # boxes, an unmanaged pfSense appliance, two beaconable Linux boxes.
        boxes = [
            {"name": "dc01", "last_octet": 2, "template": "base-windows-server"},
            {"name": "win01", "last_octet": 3, "template": "base-windows-server"},
            {"name": "fw01", "last_octet": 4, "template": "base-pfsense-fix",
             "unmanaged": True},
            {"name": "web01", "last_octet": 5, "template": "base-ubuntu24.04-fix"},
            {"name": "dns01", "last_octet": 6, "template": "base-debian13-lite-fix"},
        ]
        plan = dict(beacon.beacon_plan(boxes))
        self.assertNotIn("dc01", plan)          # windows
        self.assertNotIn("win01", plan)         # windows
        self.assertNotIn("fw01", plan)          # unmanaged pfSense
        self.assertIn("web01", plan)
        self.assertIn("dns01", plan)

    def test_an_uncovered_lineup_fails_loudly_instead_of_planting_zero(self):
        boxes = [{"name": "dc01", "last_octet": 2, "template": "base-windows-server"}]
        with self.assertRaises(RuntimeError) as ctx:
            beacon.plant_team_beacons({"team1": {"identifier": "101"}}, boxes, {})
        self.assertIn("no beaconable Linux box", str(ctx.exception))
        self.assertIn("0/0", str(ctx.exception))  # names the silent-degradation sentence


# D9 ---------------------------------------------------------------------------
class PasswordHandling(unittest.TestCase):
    def test_box_sudo_keeps_the_password_out_of_argv(self):
        pw = "a'b;c$(d)"
        argv, stdin_text = scrim.box_sudo_stdin(["ssh", "host"], "user@192.0.2.4", pw,
                                                "systemctl stop nginx")
        self.assertNotIn(pw, " ".join(argv))
        self.assertIn("sudo -S", argv[-1])
        self.assertEqual(stdin_text, pw + "\nsystemctl stop nginx")

    def test_no_password_interpolation_into_a_shell_string_remains(self):
        src = (_REPO / "run-agent-scrim.py").read_text()
        self.assertNotIn("echo %s | sudo -S", src)
        self.assertNotIn('creds["BOX_PW"], unit', src)


# D10 --------------------------------------------------------------------------
class ResumeGuardAndManifest(unittest.TestCase):
    def test_resume_window_threshold(self):
        self.assertFalse(scrim.resume_window_ok(5))
        self.assertFalse(scrim.resume_window_ok(21))
        self.assertTrue(scrim.resume_window_ok(22))
        self.assertTrue(scrim.resume_window_ok(90))

    def test_resume_refusal_message_and_force_override(self):
        self.assertIsNone(scrim.resume_refusal(45))
        refused = scrim.resume_refusal(5)
        self.assertIsNotNone(refused)
        self.assertIn("--force-resume", refused)
        self.assertIsNone(scrim.resume_refusal(5, force=True))

    def test_resume_intent_is_inherited_from_the_manifest(self):
        args = SimpleNamespace(keep_range=False, blue_watchdog=False)
        scrim.resume_intent(args, {"keep_range": True, "blue_watchdog": True})
        self.assertTrue(args.keep_range)
        self.assertTrue(args.blue_watchdog)
        # an explicit flag on the resume is still honoured
        args = SimpleNamespace(keep_range=True, blue_watchdog=False)
        scrim.resume_intent(args, {})
        self.assertTrue(args.keep_range)
        self.assertFalse(args.blue_watchdog)

    def test_manifest_round_trips_with_the_phase_marker(self):
        rd = Path(tempfile.mkdtemp())
        args = SimpleNamespace(competition="demo", teams=2, duration_min=90,
                               keep_range=True, blue_watchdog=False)
        scrim.record_phase(rd, args, "stage_red")
        man = scrim.load_manifest(rd)
        self.assertEqual(man["phase"], "stage_red")
        self.assertIsNone(man["t0"])
        self.assertTrue(man["keep_range"])
        self.assertEqual(stat.S_IMODE((rd / scrim.RUN_MANIFEST).stat().st_mode), 0o600)
        scrim.record_phase(rd, args, "event", t0=1234.5)
        self.assertEqual(scrim.load_manifest(rd)["t0"], 1234.5)

    def test_missing_manifest_is_empty_not_an_error(self):
        self.assertEqual(scrim.load_manifest(Path(tempfile.mkdtemp())), {})

    def test_the_phase_marker_is_written_before_stage_red(self):
        # stage_red deploys red01 for tens of minutes; a driver that dies in there must
        # still leave something --resume-event can reason about.
        src = (_REPO / "run-agent-scrim.py").read_text()
        # the indented call site in main(), not the `def stage_red(...)` definition
        self.assertLess(src.index('record_phase(run_dir, args, "stage_red")'),
                        src.index("\n    stage_red(args, comp, creds, run_dir)"))


# D11 --------------------------------------------------------------------------
class AuthoredCompSweepMarker(unittest.TestCase):
    def test_a_swept_template_does_not_leak_the_postclone_marker(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "competitions").mkdir()
        src = tmp / "tmpl"
        src.mkdir()
        (src / "Compfile").write_text("name tmpl\nscenario x\n")
        (src / ".postclone-swept").write_text("")   # the marker deploy.py actually writes
        (src / ".phase6-swept").write_text("")      # the stale name this file used to skip
        old_repo = scrim.REPO
        scrim.REPO = tmp
        try:
            scrim.stage_author(SimpleNamespace(new="demo", from_template=str(src)))
        finally:
            scrim.REPO = old_repo
        dst = tmp / "competitions" / "demo"
        self.assertTrue((dst / "Compfile").exists())
        # pre-fix the REAL marker was copied, so the new competition claimed to be
        # post-clone swept and deploy skipped the sweep the fresh range needs.
        self.assertFalse((dst / ".postclone-swept").exists())

    def test_the_skip_condition_names_the_postclone_marker(self):
        src = (_REPO / "run-agent-scrim.py").read_text()
        self.assertIn('if item.name == ".postclone-swept"', src)
        self.assertNotIn('if item.name == ".phase6-swept"', src)


class RedReachabilityGate(unittest.TestCase):
    """D13: pre-T0 gate that red01 can dial a box on every team.

    scale8-soak-2026-10-02: routed red01 could not reach any satellite team box (15/15
    cred_sprays, both db_attacks "unreachable over SSH", 0 footholds) and the run went
    40 minutes before anyone noticed. `verify --red-identity` only proved red reached
    ONE box on its own segment, and stage_verify runs before red exists, so nothing
    checked red's path across the range before T0.

    The gate must (a) run the every-team verify, (b) fail the run rather than warn, and
    (c) stay out of a masq-mode run, where red shares the team gateways and has no path
    of its own to prove.
    """

    def _args(self, mode=None, red_ip="10.0.0.196"):
        return SimpleNamespace(red_mode=mode, red_ip=red_ip)

    @contextlib.contextmanager
    def _repo_rooted(self):
        """Point scrim.REPO at a temp root so `comp.relative_to(REPO)` resolves."""
        with tempfile.TemporaryDirectory() as tmp:
            comp = Path(tmp) / "competitions" / "c1"
            comp.mkdir(parents=True)
            with mock.patch.object(scrim, "REPO", Path(tmp)):
                yield comp

    def test_routed_run_checks_every_team_and_fails_hard(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with self._repo_rooted() as comp, \
                mock.patch.object(scrim, "run", side_effect=fake_run), \
                mock.patch.object(scrim, "_verify_flags", return_value=[]):
            scrim.verify_red_reaches_teams(
                self._args(), comp, {"ENGINE_IP": "10.0.0.252", "ADMIN_PW": "pw"})

        cmd, kw = calls[0]
        self.assertIn("--red-teams", cmd)
        self.assertEqual(cmd[cmd.index("--red-teams") + 1], "all")
        self.assertIn("--red-identity", cmd)
        self.assertEqual(cmd[cmd.index("--red-ip") + 1], "10.0.0.196")
        self.assertIs(kw.get("check"), True,
                      "a range red cannot reach must stop the run, not warn")

    def test_masq_mode_is_skipped(self):
        calls = []
        with mock.patch.object(scrim, "run",
                               side_effect=lambda *a, **k: calls.append(a)), \
                mock.patch.object(scrim, "_verify_flags", return_value=[]):
            scrim.verify_red_reaches_teams(
                self._args(mode="masq"), Path("/tmp/comp"),
                {"ENGINE_IP": "10.0.0.252", "ADMIN_PW": "pw"})
        self.assertEqual(calls, [], "masq red shares the gateways: nothing to prove")

    def test_default_mode_is_treated_as_routed(self):
        # bad-auto's default is routed, so an unset --red-mode must still be gated.
        calls = []
        with self._repo_rooted() as comp, \
                mock.patch.object(scrim, "run",
                                  side_effect=lambda *a, **k: calls.append(a)), \
                mock.patch.object(scrim, "_verify_flags", return_value=[]):
            scrim.verify_red_reaches_teams(
                self._args(), comp, {"ENGINE_IP": "10.0.0.252", "ADMIN_PW": "pw"})
        self.assertEqual(len(calls), 1)


class RedTeardownAssertion(unittest.TestCase):
    """D14: red01 must not survive teardown, and a failed destroy must not read as DONE.

    scale8-soak-2026-10-02: red01 (998) outlived `badauto destroy` and had to be removed
    by hand. The stage ran with check=False and never looked at the result or at the
    cluster, so a destroy that removed nothing was indistinguishable from one that
    worked — the driver printed DONE while a red box holding the beacon controller and
    its LLM key stayed up on the range. `badauto destroy` follows config.yaml's
    deploy.red_vmid by design, so a stale config silently targets the wrong vmid.
    """

    def _args(self, red_vmid=998):
        return SimpleNamespace(red_vmid=red_vmid, competition="c1")

    def _run(self, rc=0, still=False, configured=998, red_vmid=998):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(returncode=rc, stdout="", stderr="")

        with mock.patch.object(scrim, "run", side_effect=fake_run), \
                mock.patch.object(scrim, "log"), \
                mock.patch.object(scrim, "_config_red_vmid", return_value=configured), \
                mock.patch.object(scrim, "_red_vm_still_exists",
                                  return_value=(True if still else False)):
            scrim.teardown_red(self._args(red_vmid), {})
        return calls

    def test_destroy_is_invoked_for_this_competition(self):
        calls = self._run()
        self.assertIn("destroy", calls[0])
        self.assertIn("c1", calls[0])

    def test_surviving_red01_raises(self):
        # The soak's exact outcome: rc=0, VM still there.
        with self.assertRaises(RuntimeError) as raised:
            self._run(rc=0, still=True)
        msg = str(raised.exception)
        self.assertIn("STILL PRESENT", msg)
        self.assertIn("998", msg)

    def test_failed_destroy_raises_with_the_rc(self):
        with self.assertRaises(RuntimeError) as raised:
            self._run(rc=1)
        msg = str(raised.exception)
        self.assertIn("badauto destroy failed", msg)
        self.assertIn("rc=1", msg)

    def test_unverifiable_is_not_silently_a_pass(self):
        # Not proof of failure (badauto may well have deleted it) but it must be said out
        # loud rather than folded into a clean teardown.
        lines = []
        with mock.patch.object(scrim, "run",
                               side_effect=lambda *a, **k: SimpleNamespace(
                                   returncode=0, stdout="", stderr="")), \
                mock.patch.object(scrim, "log", side_effect=lines.append), \
                mock.patch.object(scrim, "_config_red_vmid", return_value=None), \
                mock.patch.object(scrim, "_red_vm_still_exists", return_value=None):
            scrim.teardown_red(self._args(), {})
        self.assertTrue(any("could not be verified gone" in l for l in lines), lines)

    def test_stale_config_vmid_is_called_out(self):
        # config.yaml pointing at another run's vmid is what lost red01 in the first place.
        lines = []
        with mock.patch.object(scrim, "run",
                               side_effect=lambda *a, **k: SimpleNamespace(
                                   returncode=0, stdout="", stderr="")), \
                mock.patch.object(scrim, "log", side_effect=lines.append), \
                mock.patch.object(scrim, "_config_red_vmid", return_value=999), \
                mock.patch.object(scrim, "_red_vm_still_exists", return_value=False):
            scrim.teardown_red(self._args(red_vmid=998), {})
        self.assertTrue(any("follows config.yaml" in l for l in lines), lines)

    def test_the_assertion_uses_the_runs_own_vmid_when_config_is_silent(self):
        seen = []
        with mock.patch.object(scrim, "run",
                               side_effect=lambda *a, **k: SimpleNamespace(
                                   returncode=0, stdout="", stderr="")), \
                mock.patch.object(scrim, "log"), \
                mock.patch.object(scrim, "_config_red_vmid", return_value=None), \
                mock.patch.object(scrim, "_red_vm_still_exists",
                                  side_effect=lambda v: seen.append(v) or False):
            scrim.teardown_red(self._args(red_vmid=1234), {})
        self.assertEqual(seen, [1234])


# D12 --------------------------------------------------------------------------
class PyflakesClean(unittest.TestCase):
    FILES = ("run-agent-scrim.py", "scrim-report.py", "beacon_ops.py", "run-schedule.py")

    def test_owned_files_are_pyflakes_clean(self):
        if importlib.util.find_spec("pyflakes") is None:
            self.skipTest("pyflakes not installed")
        out = subprocess.run([sys.executable, "-m", "pyflakes", *self.FILES],
                             capture_output=True, text=True, cwd=_REPO)
        self.assertEqual(out.stdout.strip(), "", out.stdout)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
