"""Scrim harness stages that the live runs rarely reach, exercised with no infra: the
generated blue helper scripts are syntax-checked, the run-folder / resume rules run against a
temp competition, the scoreboard monitor and evidence capture parse canned Quotient bodies,
and the red tunnel argv is built without spawning anything. (release 0.2.0 split coverage)"""

import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scrim import (blue_agent, blue_helpers, blue_prompt, blue_watchdog, core, evidence,  # noqa: E402
                   quotient_api, red_tunnel, run_manifest, scoreboard_monitor, test_folder)
from scrim.runfiles import FINAL_SCOREBOARD, SCOREBOARD_STATE  # noqa: E402


def _args(run_dir, **kw):
    base = dict(competition="c1", teams=2, duration_min=90, blue_model="m", red_model="m",
                reasoning_effort="minimal", llm_base_url="https://openrouter.ai/api/v1",
                blue_base_url="https://openrouter.ai/api/v1", red_ip="10.0.0.198",
                run_dir=None if run_dir is None else str(run_dir), keep_range=False, blue_watchdog=False, resume_event=False,
                blue2_base_url=None, blue3_base_url=None, blue4_base_url=None,
                red_tunnel="auto", red_vmid=None)
    base.update(kw)
    return Namespace(**base)


CREDS = {"ENGINE_IP": "10.0.0.50", "ADMIN_PW": "adminpw", "INJECT_PW": "injpw", "BOX_PW": "boxpw",
         "BOX_USER": "obsadmin", "KEY_PATH": "/nonexistent/key", "VM_USER": "ops",
         "TEAM1_PW": "t1pw", "TEAM2_PW": "t2pw", "TEAM1_ID": "101", "TEAM2_ID": "102"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home"),
                                            "OPENROUTER_API_KEY": "fake-key-not-real"})
        home.start()
        self.addCleanup(home.stop)
        self.root = self.tmp / "repo"
        self.comp = self.root / "competitions" / "c1"
        shutil.copytree(REPO / "competitions" / "agent-scrim", self.comp)
        (self.comp / ".deploy_state.json").write_text(json.dumps(
            {"admin_password": "adminpw", "box_password": "boxpw", "inject_password": "injpw",
             "box_creds": {"obsadmin": "boxpw"}}))
        (self.comp / "teams.json").write_text(json.dumps(
            {"team1": {"password": "t1pw", "identifier": 101},
             "team2": {"password": "t2pw", "identifier": 102}}))
        (self.comp / "credentials.txt").write_text("Quotient http://10.0.0.50\n")
        p = mock.patch.object(core, "REPO", self.root)
        p.start()
        self.addCleanup(p.stop)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()


class GeneratedScripts(Base):
    def test_blue_workdir_files_render_and_parse(self):
        args = _args(None)
        test_folder.resolve_run_dir(self.comp, args)
        args.run_dir = str(self.run_dir)
        blue_agent.stage_blues(args, self.comp, self.run_dir, CREDS, time.time())
        for n in (1, 2):
            wd = self.run_dir / f"blue-team{n}"
            for sh in ("mybox", "scorch", "qlogin", "myscore", "submit-inject"):
                r = subprocess.run(["bash", "-n", str(wd / sh)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{sh}: {r.stderr}")
            py_compile.compile(str(wd / "score.py"), cfile=str(self.tmp / "s.pyc"), doraise=True)
            env = (wd / "scrim.env").read_text()
            self.assertIn(f"MY_TID=10{n}", env)
            self.assertEqual(oct((wd / "scrim.env").stat().st_mode & 0o777), "0o600")
            cfg = (wd / "opencode.jsonc").read_text()
            self.assertNotIn("{BASE_URL}", cfg)
            self.assertNotIn("{MODEL_ID}", cfg)
            self.assertNotIn("{PROVIDER_KEY}", cfg)
        self.assertTrue(json.loads(
            (Path(args.test_dir) / "test.json").read_text())["agents"]["blue"]["present"])

    def test_watchdog_script_is_valid_shell(self):
        s = blue_watchdog.watchdog_script(["ssh", "http", "mysql", "no-such"], "p'w d")
        r = subprocess.run(["bash", "-n"], input=s, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RESTORED", s)

    def test_cycle_prompt_renders_for_every_team(self):
        args = _args(self.run_dir)
        for n in (1, 2):
            p = blue_prompt.blue_cycle_prompt(n, CREDS, args, 5, 85, "delta", "inj", "nb", "tail")
            self.assertIn(f"Blue Team {n}", p)
            self.assertIn(f"192.168.10{n}.0/24", p)
            self.assertNotIn("adminpw", p)

    def test_scoreboard_delta(self):
        self.assertIn("no scoreboard", blue_prompt.scoreboard_delta(self.run_dir, "team1"))
        rec = lambda t, up: json.dumps({"t_plus_sec": t, "teams": {"team1": [
            {"service": "web", "up": up, "error": ""}]}})
        (self.run_dir / SCOREBOARD_STATE).write_text(rec(0, True) + "\n")
        self.assertIn("baseline", blue_prompt.scoreboard_delta(self.run_dir, "team1"))
        (self.run_dir / SCOREBOARD_STATE).write_text(rec(0, True) + "\n" + rec(300, False) + "\n")
        self.assertIn("web DOWN (new", blue_prompt.scoreboard_delta(self.run_dir, "team1"))


class RunFolderAndResume(Base):
    def test_resolve_run_dir_is_stable_across_resume(self):
        a1 = _args(None)
        r1, t1 = test_folder.resolve_run_dir(self.comp, a1)
        self.assertEqual(r1, t1)
        self.assertEqual(t1.parent.name, ".automated-tests")
        # a resume reuses the folder; a second FRESH run mints its own (`-run2`)
        r2, t2 = test_folder.resolve_run_dir(self.comp, _args(None, resume_event=True))
        self.assertEqual((r1, t1), (r2, t2))
        r2b, t2b = test_folder.resolve_run_dir(self.comp, _args(None))
        self.assertNotEqual(t2b, t1)
        self.assertTrue(t2b.name.endswith("-run2"))
        override = self.tmp / "elsewhere"
        a3 = _args(str(override))
        r3, t3 = test_folder.resolve_run_dir(self.comp, a3)
        self.assertEqual((r3, t3), (override, t1))

    def test_manifest_and_resume_rules(self):
        args = _args(self.run_dir, keep_range=True, blue_watchdog=True)
        run_manifest.record_phase(self.run_dir, args, "event", t0=123.0)
        m = run_manifest.load_manifest(self.run_dir)
        self.assertEqual((m["phase"], m["t0"], m["keep_range"]), ("event", 123.0, True))
        resumed = run_manifest.resume_intent(_args(self.run_dir), m)
        self.assertTrue(resumed.keep_range and resumed.blue_watchdog)
        self.assertEqual(run_manifest.load_manifest(self.tmp / "none"), {})
        self.assertIsNone(run_manifest.resume_refusal(60))
        self.assertIn("refused", run_manifest.resume_refusal(5))
        self.assertIsNone(run_manifest.resume_refusal(5, force=True))
        self.assertTrue(oct((self.run_dir / "run.json").stat().st_mode & 0o777).endswith("600"))

    def test_cli_error_paths(self):
        def run(*a):
            return subprocess.run([sys.executable, str(REPO / "run-agent-scrim.py"), *a],
                                  capture_output=True, text=True, cwd=REPO, timeout=60)
        self.assertNotEqual(run().returncode, 0)
        self.assertIn("invalid competition name", run("--competition", "a/b").stderr)
        self.assertIn("no such competition", run("--competition", "zz-nope").stderr)
        self.assertIn("no such competition", run("--competition", "zz-nope", "--resume-event").stderr)
        self.assertFalse((REPO / "competitions" / "zz-nope").exists())
        self.assertNotEqual(run("--competition", "x", "--red-mode", "bad").returncode, 0)


class MonitorAndEvidence(Base):
    SERVICES = [{"ServiceName": "web", "Last10Rounds": [
        {"Checks": []}, {"Checks": [{"Result": True}]}]},
        {"ServiceName": "ssh", "Last10Rounds": [
            {"Checks": [{"Result": False, "Error": "refused"}]}]}]

    def _fake_run(self, cmd, **kw):
        url = next((c for c in cmd if str(c).startswith("http://")), "")
        out = ""
        if url.endswith("/api/teams"):
            out = json.dumps([{"ID": 7, "Name": "team1"}, {"ID": 8, "Name": "team2"}])
        elif "/api/services/" in url:
            out = json.dumps(self.SERVICES)
        elif url.endswith("/api/injects"):
            out = json.dumps([{"ID": 1, "Title": "x"}])
        elif url.endswith("/api/login"):
            for i, c in enumerate(cmd):
                if c == "-c":
                    Path(cmd[i + 1]).write_text("cookie")
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def test_monitor_loop_one_tick(self):
        args = _args(self.run_dir)
        creds = {**CREDS, "RUN_DIR": str(self.run_dir)}
        stop = threading.Event()
        with mock.patch.object(quotient_api.subprocess, "run", self._fake_run), \
                mock.patch.object(scoreboard_monitor.red_link, "pull_red_snapshot", return_value=False), \
                mock.patch.object(scoreboard_monitor.llm_probe, "red_llm_watch"), \
                mock.patch.object(stop, "wait", lambda t: stop.set()):
            scoreboard_monitor.monitor_loop(args, creds, time.time(), stop)
        recs = [json.loads(l) for l in (self.run_dir / SCOREBOARD_STATE).read_text().splitlines()]
        self.assertEqual(len(recs), 1)
        rows = {r["service"]: r["up"] for r in recs[0]["teams"]["team1"]}
        self.assertEqual(rows, {"web": True, "ssh": False})  # empty in-flight round skipped
        self.assertIn("DOWN ssh", (self.run_dir / "monitor.log").read_text())

    def test_stage_capture_writes_final_scoreboard_and_engine_copy(self):
        creds = {**CREDS, "RUN_DIR": str(self.run_dir)}
        args = _args(self.run_dir)
        with mock.patch.object(quotient_api.subprocess, "run", self._fake_run), \
                mock.patch.object(evidence, "procs"), mock.patch.object(evidence.subprocess, "run",
                                                                        self._fake_run):
            try:
                evidence.stage_capture(args, creds)
            except Exception as e:  # pause/other steps need the live engine; the dump is first
                self.skipTest(f"post-dump steps need live engine: {e!r}")
        ev = self.run_dir / "evidence"
        final = json.loads((ev / FINAL_SCOREBOARD).read_text())
        self.assertEqual(final["services"]["team1"][1]["service"], "ssh")
        copied = evidence.capture_engine_evidence(ev)
        self.assertTrue(any(Path(p).name == FINAL_SCOREBOARD for p in copied))

    def test_capture_blue_evidence_globs(self):
        wd = self.run_dir / "blue-team1"
        (wd / "submissions").mkdir(parents=True)
        for f in ("LOG.md", "NOTEBOOK.md", "REPORT.md", "sub.md", "sub-2.txt"):
            (wd / f).write_text("x")
        ev = self.run_dir / "evidence"
        written = evidence.capture_blue_evidence(self.run_dir, ev, 2)
        names = {Path(p).name for p in written}
        self.assertTrue({"LOG.md", "REPORT.md", "sub.md", "sub-2.txt"} <= names)
        self.assertTrue((ev / "blue-team1" / "submissions").is_dir())

    def test_inject_reanchor_plan_and_red_tunnel_argv(self):
        from scrim import inject_sync
        ups, missing = inject_sync._inject_reanchor_plan(
            [{"title": "A", "open_time": "o", "due_time": "d", "close_time": "c"},
             {"title": "B", "open_time": "o", "due_time": "d", "close_time": "c"}],
            [{"ID": 3, "Title": "A", "InjectFileNames": ["f.pdf"]}])
        self.assertEqual((ups[0][0], ups[0][2], missing), ("3", ["f.pdf"], ["B"]))
        t = red_tunnel.RedTunnel("http://100.64.0.9:8080/v1", "10.0.0.198", "/k", "ops")
        self.assertEqual((t.remote_port, t.target), (8180, "100.64.0.9:8080"))
        with mock.patch.object(red_tunnel.subprocess, "Popen") as po:
            t._spawn()
        argv = po.call_args[0][0]
        self.assertIn("8180:100.64.0.9:8080", argv)
        self.assertEqual(argv[-1], "ops@10.0.0.198")
        self.assertEqual(t.red_base_url(), "http://localhost:8180/v1")
        self.assertIsNone(red_tunnel.maybe_start_red_tunnel(_args(self.run_dir)))


if __name__ == "__main__":
    unittest.main()
