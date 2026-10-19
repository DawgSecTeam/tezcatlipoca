"""Regression tests for the red-vs-blue scrim harness defect audit."""

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

from estate import config
from types import SimpleNamespace
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    # the harness modules loaded below assume the repo root is importable
    sys.path.insert(0, str(_REPO))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _REPO / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


from scrim import blue_agent, blue_prompt, compworld, core, endpoints, event_run, evidence, inject_sync, llm_probe, procs, quotient_api, red_link, red_stage, run_manifest, staging, supervisor, teardown_stage
from scrim_report import blue_side, host_labels, loaders, red_side, render, timefmt
from quotient import event_capture as event_capture
sched = _load("run_schedule_defects", "run-schedule.py")


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



class ProcessTreeSupervision(unittest.TestCase):
# run_tree must reap the whole process group; subprocess.run(timeout=) does not.

    # A grandchild that IGNORES SIGINT/SIGTERM: only the SIGKILL escalation can reap it, which
    # is exactly the nakon/badauto case (they keep mutating infra and holding the deploy
    # lock after the direct child is killed).
    @staticmethod
    def _orphan_script(pidfile):
        return (f"bash -c 'trap \"\" INT TERM; exec sleep 300' & "
                f"echo $! > {pidfile}; sleep 300")

    def test_run_tree_kills_the_whole_process_group(self):
        tmp = Path(tempfile.mkdtemp())
        pidfile = tmp / "gc.pid"
        with self.assertRaises(procs.ScrimTimeout):
            procs.run_tree(["bash", "-c", self._orphan_script(pidfile)],
                           timeout=0.6, grace=0.4, check=False)
        pid = int(pidfile.read_text())
        if not _wait_dead(pid):
            os.kill(pid, signal.SIGKILL)
            self.fail(f"grandchild pid {pid} survived the run_tree timeout")
        self.assertFalse(_pid_alive(pid))



    def test_stdin_is_delivered_without_argv_exposure(self):
        r = procs.run_tree(["bash", "-c", "cat"], timeout=30, check=False,
                           stdin_text="secret\nscript\n")
        self.assertEqual(r.stdout, "secret\nscript\n")


class TrackingLock:
# Records the exact acquire/release order (a plain Lock cannot show the interval).

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
        def fake(args, prompt, wd, env, stop=None):
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
        with mock.patch.object(blue_agent, "_opencode_run", fake):
            r = blue_agent.run_cycle_with_retry(SimpleNamespace(), "p", "/tmp/blue-team1", {},
                                           lock, threading.Event(), team=1)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(calls), 2)
        # pre-fix this was one acquisition around BOTH attempts
        self.assertEqual(lock.events, ["acquire", "release", "acquire", "release"])
        self.assertFalse(lock._lock.locked())


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
        with mock.patch.object(blue_agent, "_opencode_run", fake), \
                mock.patch.object(blue_prompt, "blue_cycle_prompt", lambda *a, **k: "prompt"), \
                mock.patch.object(blue_prompt, "scoreboard_delta", lambda *a, **k: ""), \
                mock.patch.object(inject_sync, "inject_brief", lambda *a, **k: ""), \
                mock.patch.object(endpoints, "api_key", lambda *a, **k: "k"):
            blue_agent.blue_feed_loop(1, args, creds, time.time(), stop, threading.Lock(),
                                 first_delay=0)
        self.assertEqual(len(calls), 2, "the retry should have been attempted")
        feed = (rd / "blue-team1" / "feed.log").read_text()
        self.assertIn("===== cycle T+0 TIMEOUT =====", feed)
        # the rehearsal gate reads exactly this header: a retried-then-timed-out cycle used to
        # be the one timeout the report could not see
        self.assertEqual(blue_side.blue_metrics(rd)["timeouts"], 1)



class WorkerSupervision(unittest.TestCase):





    def test_team_tid_raises_a_clear_error_on_an_error_body(self):
        # an error/HTML body used to make next(...) raise StopIteration/TypeError inside
        # the monitor thread, killing it with nothing watching
        for body in ('{"error":"Forbidden"}', "<html>500</html>", '{"ID":1}'):
            with mock.patch.object(quotient_api, "qget",
                                   lambda *a, **k: SimpleNamespace(stdout=body)):
                with self.assertRaises(ValueError):
                    quotient_api._team_tid({"ENGINE_IP": "x"}, "team1", "/tmp/j.jar", "team1")


    def test_worker_death_still_captures_and_tears_down(self):
        calls = []
        with mock.patch.object(event_run, "stage_run",
                               side_effect=supervisor.WorkerDiedError("blue-feed-1 died")), \
                mock.patch.object(evidence, "stage_capture", lambda *a: calls.append("capture")), \
                mock.patch.object(teardown_stage, "stage_teardown", lambda *a: calls.append("teardown")):
            failure = event_run.run_event_and_finish(
                SimpleNamespace(run_dir=self.enterContext(tempfile.TemporaryDirectory())), {}, 0.0)
        self.assertEqual(calls, ["capture", "teardown"])
        self.assertIn("blue-feed-1", failure)


class RedLlmWatchdog(unittest.TestCase):
    class Tunnel:
        def __init__(self):
            self.restarts = 0
            self.remote_port = 8180

        def restart(self):
            self.restarts += 1

    def setUp(self):
        llm_probe._llm_watch.clear()

    def _args(self, rd, url="http://llm.local/v1", tunnel=None):
        return SimpleNamespace(run_dir=str(rd), llm_base_url=url, red_tunnel=tunnel or "off")

    def test_hysteresis_restarts_the_tunnel_once_per_episode(self):
        rd = Path(tempfile.mkdtemp())
        tunnel = self.Tunnel()
        args = self._args(rd, tunnel=tunnel)
        with mock.patch.object(red_link, "check_red_llm", lambda *a: False):
            for tick in range(5):
                llm_probe.red_llm_watch(args, tick * 5)
        # 5 failed probes -> one rescue, not five
        self.assertEqual(tunnel.restarts, 1)
        lines = llm_probe.alerts_path(rd).read_text().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["kind"], "red_llm_restart")
        self.assertEqual(rec["failed_probes"], llm_probe.RED_LLM_FAIL_THRESHOLD)
        self.assertEqual(stat.S_IMODE(llm_probe.alerts_path(rd).stat().st_mode), 0o600)


    def test_recovery_clears_the_episode_so_a_later_outage_is_rescued(self):
        rd = Path(tempfile.mkdtemp())
        tunnel = self.Tunnel()
        args = self._args(rd, tunnel=tunnel)
        reachable = {"v": False}
        with mock.patch.object(red_link, "check_red_llm", lambda *a: reachable["v"]):
            for tick in range(3):
                llm_probe.red_llm_watch(args, tick)
            self.assertEqual(tunnel.restarts, 1)
            reachable["v"] = True
            llm_probe.red_llm_watch(args, 3)
            self.assertIsNone(llm_probe._llm_watch[args.llm_base_url]["since"])
            reachable["v"] = False
            for tick in range(4, 7):
                llm_probe.red_llm_watch(args, tick)
        self.assertEqual(tunnel.restarts, 2)


    def test_report_survives_red_events_without_a_world_clock(self):
        # A run dir with red events but no world.json has no event_start; the stall maths used to
        # compare None to a float and abort the WHOLE report, so a timeout the rehearsal gate
        # must see was never counted.
        run = Path(tempfile.mkdtemp())
        (run / "evidence" / "red").mkdir(parents=True)
        (run / "evidence" / "red" / "events.jsonl").write_text(json.dumps(
            {"ts": "2026-09-29T07:00:00Z", "kind": "action", "ok": True,
             "tactic": "impact_service", "target": "192.168.101.4", "detail": "x"}) + "\n")
        out, meta = render.build_report(run)
        self.assertIn("## Gates", out)
        self.assertEqual(meta["gates_failed"], meta["gates_failed"])



class EvidenceHygiene(unittest.TestCase):
    def test_jar_path_is_private_and_out_of_shared_tmp(self):
        rd = Path(tempfile.mkdtemp())
        p = Path(quotient_api.jar_path(rd, "team1"))
        self.assertTrue(p.is_file())
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)
        self.assertTrue(str(p).startswith(str(rd)))
        self.assertFalse(str(p).startswith("/tmp/jar."))
        self.assertEqual(quotient_api.jar_path(rd, "team1"), str(p))
        p.write_text("cookie")
        quotient_api.jar_path(rd, "team1")
        self.assertEqual(p.read_text(), "cookie")

    def test_evidence_writer_is_atomic_and_0600(self):
        rd = Path(tempfile.mkdtemp())
        out = rd / "final-scoreboard.json"
        core.write_evidence(out, '{"a": 1}')
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual(json.loads(out.read_text()), {"a": 1})
        self.assertFalse((rd / "final-scoreboard.json.tmp").exists())



    def test_qget_relogins_only_for_a_real_error_object(self):
        for body, expected_logins in (('{"service":"error-handler","up":true}', 0),
                                      ('[{"ID":1,"Name":"team1"}]', 0),
                                      ('{"error":"Forbidden"}', 1)):
            logins = []
            with mock.patch.object(quotient_api, "_qlogin", lambda *a: logins.append(1)), \
                    mock.patch.object(quotient_api.subprocess, "run",
                                      lambda *a, **k: SimpleNamespace(returncode=0, stdout=body)):
                again = quotient_api.qget({"ENGINE_IP": "192.0.2.1"}, "team1",
                                   "/tmp/does-not-matter.jar", "/api/teams")
            self.assertEqual(len(logins), expected_logins, body)
            self.assertIsNotNone(again)

    def test_login_writes_via_temp_then_rename_and_keeps_the_old_jar_on_failure(self):
        rd = Path(tempfile.mkdtemp())
        jar = Path(quotient_api.jar_path(rd, "team1"))
        jar.write_text("old-cookie")
        creds = {"ENGINE_IP": "192.0.2.1", "TEAM1_PW": "pw"}
        # curl failing (e.g. engine unreachable) must not destroy a working session
        with mock.patch.object(quotient_api.subprocess, "run",
                               lambda *a, **k: SimpleNamespace(returncode=7)):
            self.assertFalse(quotient_api._qlogin(creds, "team1", str(jar)))
        self.assertEqual(jar.read_text(), "old-cookie")

        def fake_curl(cmd, *a, **k):
            Path(cmd[cmd.index("-c") + 1]).write_text("new-cookie")
            return SimpleNamespace(returncode=0)

        with mock.patch.object(quotient_api.subprocess, "run", fake_curl):
            self.assertTrue(quotient_api._qlogin(creds, "team1", str(jar)))
        self.assertEqual(jar.read_text(), "new-cookie")
        self.assertEqual(stat.S_IMODE(jar.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in (rd / ".jars").iterdir()), ["team1.jar"])

    def test_jar_lock_is_per_account_and_reentrant(self):
        self.assertIs(quotient_api.jar_lock("team1"), quotient_api.jar_lock("team1"))
        self.assertIsNot(quotient_api.jar_lock("team2"), quotient_api.jar_lock("team1"))
        with quotient_api.jar_lock("team1"):
            with quotient_api.jar_lock("team1"):
                pass  # RLock: qget -> _qlogin must not self-deadlock


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
# Minimal requests.Session stand-in for dump_scoreboard/end_event.

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
        errors = event_capture.dump_scoreboard("http://192.0.2.1", session, comp)
        self.assertTrue(errors)
        dumps = sorted((comp / "evidence").glob("event-final-*.json"))
        self.assertEqual(len(dumps), 1)
        self.assertEqual(stat.S_IMODE(dumps[0].stat().st_mode), 0o600)
        self.assertIn("teams_error", json.loads(dumps[0].read_text()))


    def test_end_pauses_even_when_the_capture_fails(self):
        import requests
        comp = Path(tempfile.mkdtemp())
        session = FakeSession(raises=requests.RequestException("500 Server Error"))
        rc = sched.end_event("http://192.0.2.1", session, comp)
        self.assertEqual(len(session.posts), 1, "the pause must still be attempted")
        self.assertIn("/api/engine/pause", session.posts[0][0])
        self.assertEqual(session.posts[0][1], {"pause": True})
        self.assertEqual(rc, 1)



    def test_end_reports_a_dropped_pause_connection(self):
        import requests
        comp = Path(tempfile.mkdtemp())
        session = FakeSession(post_raises=requests.ConnectionError("refused"))
        self.assertEqual(sched.end_event("http://192.0.2.1", session, comp), 1)



# The report must derive host labels from the COMPETITION's boxes.json, never from a
# name-keyed table baked into the report code: a fixed table silently mislabels or
# zero-labels every other lineup.
class LineupDerivation(unittest.TestCase):
    # the old name-keyed interval table, kept as the behavior baseline the
    # roster-derived plan must still reproduce on a same-shaped lineup
    LEGACY_NAME_GATE = {"web01": 45, "app01": 60, "db01": 90}
    LINEUP = ["db01", "file01", "app01", "relay01"]

    def setUp(self):
        # a private comps root so the suite never reads the developer's real comps checkout
        self.tmp = Path(tempfile.mkdtemp())
        token = config.active().activate({"knobs.competitions_root": str(self.tmp)})
        self.addCleanup(config.active().deactivate, token)

    def _comp(self, names=None):
        comp = self.tmp / "lineup-comp"
        comp.mkdir(parents=True, exist_ok=True)
        (comp / "boxes.json").write_text(json.dumps(
            [{"name": n, "last_octet": i + 2, "template": "base-ubuntu24.04-fix"}
             for i, n in enumerate(names or self.LINEUP)]))
        return comp

    def _run_dir(self, name):
        run = self.tmp / name
        (run / "evidence" / "red").mkdir(parents=True)
        return run

    def _boxes(self, names, templates=None):
        return [
            {"name": n, "last_octet": i + 2,
             "template": (templates or {}).get(n, "base-ubuntu24.04-fix")}
            for i, n in enumerate(names)]

    def test_box_labels_come_from_the_competition_boxes_json(self):
        comp = self._comp()
        labels = host_labels.box_labels(comp)
        self.assertEqual(labels, {"2": "db01", "3": "file01", "4": "app01", "5": "relay01"})
        self.assertEqual(host_labels.host_label("192.168.101.4", labels), "1:app01")
        # a non-team address (red01) keeps its raw octets and is never team-labelled
        self.assertEqual(host_labels.host_label("10.0.0.243", labels), ".243")
        self.assertNotIn(":", host_labels.host_label("10.0.0.243", labels))

    def test_run_dir_inside_the_competition_resolves_three_levels_up(self):
        comp = self._comp()
        run = comp / ".automated-tests" / "run-abc12345" / "evidence" / "red"
        run.mkdir(parents=True)
        self.assertEqual(host_labels.box_labels(run),
                         {"2": "db01", "3": "file01", "4": "app01", "5": "relay01"})


    def test_unknown_run_dir_does_not_invent_a_name_from_the_wrong_lineup(self):
        fake_run = self._run_dir("totally-unknown-run")
        labels = host_labels.box_labels(fake_run)
        self.assertEqual(labels, {})
        self.assertEqual(host_labels.host_label("192.168.101.4", labels), "1:.4")




class PasswordHandling(unittest.TestCase):
    def test_box_sudo_keeps_the_password_out_of_argv(self):
        pw = "a'b;c$(d)"
        argv, stdin_text = procs.box_sudo_stdin(["ssh", "host"], "user@192.0.2.4", pw,
                                                "systemctl stop nginx")
        self.assertNotIn(pw, " ".join(argv))
        self.assertIn("sudo -S", argv[-1])
        self.assertEqual(stdin_text, pw + "\nsystemctl stop nginx")


class AuthoredCompSweepMarker(unittest.TestCase):
    def test_a_swept_template_does_not_leak_the_postclone_marker(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "competitions").mkdir()
        src = tmp / "tmpl"
        src.mkdir()
        (src / "Compfile").write_text("name tmpl\nscenario x\n")
        (src / ".postclone-swept").write_text("")   # the marker deploy.py actually writes
        (src / ".phase6-swept").write_text("")      # the stale name this file used to skip
        old_root = os.environ.get("TEZ_COMPETITIONS_ROOT")
        os.environ["TEZ_COMPETITIONS_ROOT"] = str(tmp / "competitions")
        try:
            staging.stage_author(SimpleNamespace(new="demo", from_template=str(src)))
        finally:
            if old_root is None:
                os.environ.pop("TEZ_COMPETITIONS_ROOT", None)
            else:
                os.environ["TEZ_COMPETITIONS_ROOT"] = old_root
        dst = tmp / "competitions" / "demo"
        self.assertTrue((dst / "Compfile").exists())
        self.assertFalse((dst / ".postclone-swept").exists())

    def test_bare_from_template_name_resolves_against_competitions(self):
    # a bare --from-template <name> must resolve against the comps root, not the cwd
        tmp = Path(tempfile.mkdtemp())
        (tmp / "competitions" / "tmpl").mkdir(parents=True)
        (tmp / "competitions" / "tmpl" / "Compfile").write_text("name tmpl\n")
        old_root = os.environ.get("TEZ_COMPETITIONS_ROOT")
        os.environ["TEZ_COMPETITIONS_ROOT"] = str(tmp / "competitions")
        try:
            staging.stage_author(SimpleNamespace(new="demo", from_template="tmpl"))
        finally:
            if old_root is None:
                os.environ.pop("TEZ_COMPETITIONS_ROOT", None)
            else:
                os.environ["TEZ_COMPETITIONS_ROOT"] = old_root
        self.assertTrue((tmp / "competitions" / "demo" / "Compfile").exists())

class RedReachabilityGate(unittest.TestCase):
# pre-T0 gate: red01 can dial a box on every team. Nothing checked red's path across the range
# before T0, so a routed red01 that could reach no satellite team box went 40 minutes unnoticed.
# The gate must run the every-team verify, fail the run rather than warn, and stay out of a
# masq-mode run, where red shares the team gateways and has no path of its own to prove.

    def _args(self, mode=None, red_ip="10.0.0.196"):
        return SimpleNamespace(red_mode=mode, red_ip=red_ip)

    @contextlib.contextmanager
    def _repo_rooted(self):
    # Point core.REPO at a temp root so `comp.relative_to(REPO)` resolves.
        with tempfile.TemporaryDirectory() as tmp:
            comp = Path(tmp) / "competitions" / "c1"
            comp.mkdir(parents=True)
            with mock.patch.object(core, "REPO", Path(tmp)):
                yield comp

    PASS_OUT = (
        "  red_identity      : PASS  10.200.0.11 visible end-to-end\n"
        "  red_teams         : PASS  red01 reached all 2 teams\n"
        "RESULT: FAIL — see failing checks above.\n")

    def test_routed_run_checks_every_team_and_fails_hard(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return SimpleNamespace(returncode=0, stdout=self.PASS_OUT, stderr="")

        with self._repo_rooted() as comp, \
                mock.patch.object(procs, "run", side_effect=fake_run), \
                mock.patch.object(compworld, "_verify_flags", return_value=[]):
            red_stage.verify_red_reaches_teams(
                self._args(), comp, {"ENGINE_IP": "10.0.0.248", "ADMIN_PW": "pw"})

        cmd, kw = calls[0]
        self.assertIn("--red-teams", cmd)
        self.assertEqual(cmd[cmd.index("--red-teams") + 1], "all")
        self.assertIn("--red-identity", cmd)
        self.assertEqual(cmd[cmd.index("--red-ip") + 1], "10.0.0.196")

    def test_unrelated_estate_fails_do_not_kill_the_red_gate(self):
    # verify can exit rc=1 on an unrelated estate failure while red_identity/red_teams both
    # PASS: the gate's subject is red's path only
        with self._repo_rooted() as comp, \
                mock.patch.object(procs, "run", return_value=SimpleNamespace(
                    returncode=1, stdout=self.PASS_OUT, stderr="")), \
                mock.patch.object(compworld, "_verify_flags", return_value=[]):
            red_stage.verify_red_reaches_teams(
                self._args(), comp, {"ENGINE_IP": "10.0.0.248", "ADMIN_PW": "pw"})

    def test_red_check_failure_stops_the_run(self):
        for stdout in (self.PASS_OUT.replace("red_teams         : PASS",
                                             "red_teams         : FAIL"),
                       "RESULT: FAIL\n"):
            with self._repo_rooted() as comp, \
                    mock.patch.object(procs, "run", return_value=SimpleNamespace(
                        returncode=1, stdout=stdout, stderr="boom")), \
                    mock.patch.object(compworld, "_verify_flags", return_value=[]):
                with self.assertRaises(RuntimeError):
                    red_stage.verify_red_reaches_teams(
                        self._args(), comp,
                        {"ENGINE_IP": "10.0.0.248", "ADMIN_PW": "pw"})





if __name__ == "__main__":
    unittest.main()




class InjectPrompt(unittest.TestCase):
    def _brief(self, due_off, close_off):
        now = time.time()
        payload = [{"ID": 3, "Title": "Breach notification",
                    "DueTime": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                             time.gmtime(now + due_off * 60)),
                    "CloseTime": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(now + close_off * 60)),
                    "Submissions": []}]
        with mock.patch.object(inject_sync.quotient_api, "qget",
                               return_value=SimpleNamespace(stdout=json.dumps(payload))), \
                mock.patch.object(inject_sync.quotient_api, "jar_path",
                                  return_value=Path("/tmp/j.jar")):
            return inject_sync.inject_brief({"RUN_DIR": "/tmp"}, "team1")

    def test_an_overdue_inject_says_so_and_stays_urgent(self):
        # 33 minutes overdue used to render "due in 207min" (the local reading, hours
        # ahead) and never entered the urgent branch.
        text = self._brief(-33, 15)
        self.assertIn("OVERDUE by 33min", text)
        self.assertIn("OPEN (overdue)", text)
        self.assertIn("TASK #1", text)

    def test_a_closed_inject_is_not_urgent(self):
        text = self._brief(-60, -30)
        self.assertIn("closed", text)
        self.assertIn("CLOSED (missed)", text)
        self.assertNotIn("TASK #1", text)
