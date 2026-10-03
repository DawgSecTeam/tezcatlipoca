"""The scrim harness writes into its run's per-run test folder (artifacts_ops.py).

Each test here pins one invariant of the harness half of the artifact contract:

  INV1  the default run dir IS `competitions/<comp>/.automated-tests/<key>`, and a resume
        resolves to the same folder (no second key, no forked evidence);
  INV2  `--run-dir` still works, with `paths.run_dir` recorded so the collector finds the
        evidence outside the test folder;
  INV3  `ensure_test(...)` runs at start with the comp's identity, stashed on `args.test_dir`;
  INV4  `record_phase` mirrors into test.json and keeps writing run.json exactly as before;
  INV5  red's and blue's identity is recorded when it is known, from the same ssh helper
        `_red_ssh_ctx` uses (bad-auto's config.yaml is a rewritten singleton at teardown time);
  INV6  teardown collects BEFORE `badauto destroy`, reports are best-effort, finalize runs
        last and on the --keep-range path too;
  INV7  the blue submission globs match what the prompt actually asks for (`sub.md`);
  INV8  the engine capture is COPIED into evidence/engine/ (scrim-report.py reads the original);
  INV9  re-recording agents/paths on a resume merges instead of erasing;
  INV10 blue is told to write the REPORT.md the collector looks for.

Offline: no network, no ssh, no Proxmox. The collector, `badauto`, and the engine are patched
wherever the point of the test is sequencing; file placement is exercised for real in tmp dirs.
"""

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import artifacts_ops as ao


def _load(name, filename):
    """tests/test_destroy_teardown.py's pattern: the harness filename has hyphens."""
    spec = importlib.util.spec_from_file_location(name, _REPO / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scrim = _load("run_agent_scrim_artifacts", "run-agent-scrim.py")

RUN_ID = "run-1a2b3c4d"
BOXES = [{"name": "dc01", "last_octet": 2, "template": "base-windows-server"},
         {"name": "web01", "last_octet": 4, "template": "base-ubuntu24.04-fix"}]


def make_comp(root, *, name="demo", run_id=RUN_ID):
    """A competition dir with just the files the harness reads at start."""
    comp = Path(root) / "competitions" / name
    comp.mkdir(parents=True)
    (comp / "Compfile").write_text(f"name {name}-2026-10-03\nscenario x\n")
    (comp / "boxes.json").write_text(json.dumps(BOXES))
    (comp / ".deploy_state.json").write_text(json.dumps({"run_id": run_id} if run_id else {}))
    return comp


def make_args(comp, **over):
    args = SimpleNamespace(competition=Path(comp).name, teams=2, duration_min=90,
                           keep_range=False, blue_watchdog=False, run_dir=None,
                           red_ip="10.0.0.198", red_vmid=None)
    for key, value in over.items():
        setattr(args, key, value)
    return args


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.comp = make_comp(self.root)

    def resolve(self, args, comp=None):
        """resolve_run_dir with the node/endpoint env a deploy would have set."""
        with mock.patch.dict(os.environ, {"TF_VAR_proxmox_node": "pve",
                                          "TF_VAR_proxmox_endpoint": "https://10.0.0.193:8006"}):
            return scrim.resolve_run_dir(comp or self.comp, args)


class RunDirResolution(TmpCase):
    """INV1-INV3: where the run writes, and where its test folder lives."""

    def test_default_run_dir_is_the_test_folder(self):
        args = make_args(self.comp)
        run_dir, test_dir = self.resolve(args)
        self.assertEqual(test_dir, self.comp / ao.DIRNAME / RUN_ID)
        self.assertEqual(run_dir, test_dir)
        self.assertTrue((test_dir / "test.json").exists())
        # INV3: stashed so later stages never re-derive it
        self.assertEqual(args.test_dir, str(test_dir))
        for sub in ao.EVIDENCE_DIRS:
            self.assertTrue((test_dir / "evidence" / sub).is_dir())
        # the collector's view of the same folder: harness evidence is right here
        targets = ao.plan_targets(ao.load_manifest(test_dir), comp_dir=self.comp)
        harness = next(t for t in targets if t["name"] == "harness")
        self.assertEqual(harness.get("root"), str(test_dir))
        self.assertNotIn("status", harness)

    def test_ensure_test_records_the_run_identity(self):
        args = make_args(self.comp)
        _run_dir, test_dir = self.resolve(args)
        man = ao.load_manifest(test_dir)
        self.assertEqual(man["kind"], "scrim")
        self.assertEqual(man["created_by"]["script"], "run-agent-scrim.py")
        self.assertEqual(man["comp"], "demo")
        self.assertEqual(man["comp_name"], "demo-2026-10-03")
        self.assertEqual(man["teams"], 2)
        self.assertEqual(man["boxes"], ["dc01", "web01"])
        self.assertEqual(man["node"], "pve")
        self.assertEqual(man["endpoint"], "https://10.0.0.193:8006")
        self.assertEqual(man["run_id"], RUN_ID)
        self.assertEqual(man["paths"]["run_dir"], str(test_dir))

    def test_untagged_comp_gets_one_stable_key(self):
        comp = make_comp(self.root, name="legacy", run_id=None)
        first = self.resolve(make_args(comp), comp=comp)
        second = self.resolve(make_args(comp), comp=comp)
        self.assertEqual(first[1], second[1])
        self.assertTrue(first[1].name.startswith("untagged-"))
        folders = sorted(p.name for p in (comp / ao.DIRNAME).iterdir() if p.is_dir())
        self.assertEqual(folders, [first[1].name])

    def test_resume_resolves_to_the_same_folder(self):
        first_run, first_test = self.resolve(make_args(self.comp))
        # a live run leaves its clock and manifest in the folder before the driver dies
        (first_run / "T0.txt").write_text(json.dumps({"t0": 1000.0, "duration_min": 90}))
        (first_run / "run.json").write_text(json.dumps({"phase": "event", "t0": 1000.0}))
        # the --resume-event call: a fresh args object, no --run-dir
        second_run, second_test = self.resolve(make_args(self.comp))
        self.assertEqual(second_run, first_run)
        self.assertEqual(second_test, first_test)
        folders = sorted(p.name for p in (self.comp / ao.DIRNAME).iterdir() if p.is_dir())
        self.assertEqual(folders, [RUN_ID], "a resume must not mint a second key")

    def test_recorded_run_dir_is_authoritative_for_the_resume(self):
        elsewhere = self.root / "elsewhere-run"
        elsewhere.mkdir()
        first = self.resolve(make_args(self.comp, run_dir=str(elsewhere)))
        self.assertEqual(first[0], elsewhere)
        second = self.resolve(make_args(self.comp, run_dir=None))
        self.assertEqual(second[0], elsewhere, "the recorded paths.run_dir is authoritative")
        self.assertEqual(second[1], first[1])
        folders = sorted(p.name for p in (self.comp / ao.DIRNAME).iterdir() if p.is_dir())
        self.assertEqual(folders, [RUN_ID])

    def test_explicit_run_dir_is_kept_and_recorded(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        run_dir, test_dir = self.resolve(make_args(self.comp, run_dir=str(elsewhere)))
        self.assertEqual(run_dir, elsewhere)
        # the test folder stays under the comp dir; only the run moves
        self.assertEqual(test_dir, self.comp / ao.DIRNAME / RUN_ID)
        self.assertTrue((test_dir / "test.json").exists())
        self.assertEqual(ao.load_manifest(test_dir)["paths"]["run_dir"], str(elsewhere))
        # ...and the collector can still find the evidence there (INV2)
        targets = ao.plan_targets(ao.load_manifest(test_dir), comp_dir=self.comp)
        harness = next(t for t in targets if t["name"] == "harness")
        self.assertEqual(harness.get("root"), str(elsewhere))
        self.assertNotIn("status", harness)


class PhaseMirror(TmpCase):
    """INV4: test.json mirrors the phase markers; run.json stays the harness's own file."""

    def test_record_phase_mirrors_into_test_json_and_keeps_run_json(self):
        args = make_args(self.comp, keep_range=True)
        run_dir, test_dir = self.resolve(args)
        scrim.record_phase(run_dir, args, "stage_red")
        man = scrim.load_manifest(run_dir)  # run.json, unchanged in shape
        self.assertEqual(man["phase"], "stage_red")
        self.assertIsNone(man["t0"])
        self.assertTrue(man["keep_range"])
        self.assertEqual(man["competition"], "demo")
        self.assertEqual(man["teams"], 2)
        self.assertEqual(man["duration_min"], 90)
        self.assertEqual(os.stat(run_dir / scrim.RUN_MANIFEST).st_mode & 0o777, 0o600)
        mirrored = ao.load_manifest(test_dir)
        self.assertEqual([p["phase"] for p in mirrored["phases"]], ["stage_red"])
        self.assertEqual(mirrored["phase"], "stage_red")
        scrim.record_phase(run_dir, args, "event", t0=1234.5)
        self.assertEqual(scrim.load_manifest(run_dir)["t0"], 1234.5)
        self.assertEqual([p["phase"] for p in ao.load_manifest(test_dir)["phases"]],
                         ["stage_red", "event"])
        self.assertEqual(ao.load_manifest(test_dir)["phases"][1]["t0"], 1234.5)

    def test_record_phase_without_a_test_dir_still_writes_run_json(self):
        """tests/test_scrim_defects.py passes a SimpleNamespace with no test_dir."""
        run_dir = self.root / "legacy-run"
        run_dir.mkdir()
        args = SimpleNamespace(competition="demo", teams=2, duration_min=90,
                               keep_range=True, blue_watchdog=False)
        scrim.record_phase(run_dir, args, "stage_red")
        self.assertEqual(scrim.load_manifest(run_dir)["phase"], "stage_red")


class AgentIdentity(TmpCase):
    """INV5/INV9: identity is recorded from the one ssh helper, and merged, never replaced."""

    def setUp(self):
        super().setUp()
        # A fake repo root + bad-auto tree, so the ssh helper reads THIS run's files without
        # ever touching the real ../bad-auto or the network.
        self.repo = self.root
        self.bad = self.root / "bad-auto"
        self.bad.mkdir()
        (self.bad / "config.yaml").write_text(
            json.dumps({"deploy": {"red_ip": "10.0.0.199"}}))
        (self.comp / "credentials.txt").write_text("engine http://10.0.0.193:8006/\n")
        for patch in (mock.patch.object(scrim, "REPO", self.repo),
                      mock.patch.object(scrim, "BAD_AUTO", self.bad),
                      mock.patch.dict(os.environ, {"TF_VAR_vm_username": "sysadmin",
                                                   "TF_VAR_proxmox_node": "pve"})):
            patch.start()
            self.addCleanup(patch.stop)

    def test_red_ssh_spec_is_the_one_helper_red_ssh_ctx_uses(self):
        args = make_args(self.comp, red_ip="10.0.0.198")  # config.yaml disagrees on purpose
        target, common, jump = scrim._red_ssh_ctx(args)
        spec, spec_common = scrim._red_ssh_spec(args)
        self.assertEqual(spec["host"], "10.0.0.199")
        self.assertEqual(spec["user"], "sysadmin")
        self.assertEqual(spec["key"], str(self.repo / "proxmox"))
        self.assertEqual(spec["jump"], jump)
        self.assertIn("ProxyCommand=ssh", jump)
        self.assertIn("10.0.0.193", jump)
        self.assertEqual(target, "sysadmin@10.0.0.199")
        self.assertEqual(common, spec_common)
        self.assertIn(spec["key"], common)

    def test_stage_red_records_the_identity_in_the_manifest(self):
        args = make_args(self.comp, run_dir=None, red_ip="10.0.0.199", red_vmid=999,
                         red_gw="10.0.0.1", red_storage="hdd", red_mode=None,
                         red_template=None, red_subnet=None, red_seg_ip=None,
                         llm_base_url="https://openrouter.ai/api/v1", red_model="m",
                         reasoning_effort=None, red_tunnel="off")
        run_dir, test_dir = self.resolve(args)
        with mock.patch.object(scrim, "run",
                               lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", "")), \
                mock.patch.object(scrim, "check_red_llm", lambda *a, **k: True), \
                mock.patch.object(scrim, "api_key", lambda *a, **k: "k"):
            # creds must be real-shaped: routed red runs the pre-T0 reachability gate,
            # which reads ENGINE_IP/ADMIN_PW (mocked run answers rc=0 for it here)
            scrim.stage_red(args, self.comp, {"ENGINE_IP": "10.0.0.193",
                                              "ADMIN_PW": "pw"}, run_dir)
        red = ao.load_manifest(test_dir)["agents"]["red"]
        self.assertTrue(red["present"])
        self.assertEqual(red["ip"], "10.0.0.199")
        self.assertEqual(red["vmid"], 999)
        self.assertEqual(red["node"], "pve")
        spec, _common = scrim._red_ssh_spec(args)
        self.assertEqual(red["ssh"], spec)
        # the collector's red target is live and dials EXACTLY this spec, not config.yaml
        targets = ao.plan_targets(ao.load_manifest(test_dir), comp_dir=self.comp)
        red_target = next(t for t in targets if t["name"] == "red01")
        self.assertEqual(red_target["ssh"], spec)
        self.assertEqual(red_target["vmid"], 999)
        scrim.record_red_agent(args)  # a resume re-records; identical, not erased
        self.assertEqual(ao.load_manifest(test_dir)["agents"]["red"], red)

    def test_agent_records_merge_without_erasing(self):
        args = make_args(self.comp, run_dir=None)
        run_dir, test_dir = self.resolve(args)
        for n in (1, 2):
            (run_dir / f"blue-team{n}").mkdir()
        scrim.record_agent(args, "red", present=True, ip="10.0.0.199", vmid=999)
        scrim.record_blue_agents(args, run_dir)
        man = ao.load_manifest(test_dir)
        self.assertEqual(man["agents"]["blue"], {"present": True, "teams": 2})
        self.assertTrue(man["agents"]["red"]["present"], "blue must not erase red")
        self.assertEqual(man["agents"]["red"]["ip"], "10.0.0.199")
        self.assertEqual(man["paths"]["blue_workdirs"],
                         [str(run_dir / "blue-team1"), str(run_dir / "blue-team2")])
        self.assertEqual(man["paths"]["engine_evidence"],
                         str(run_dir / "evidence" / "engine"))
        self.assertEqual(man["paths"]["run_dir"], str(run_dir))
        # both agent targets are collectable, not "present but no workdir recorded"
        names = [t["name"] for t in ao.plan_targets(man, comp_dir=self.comp)]
        self.assertIn("blue:blue-team1", names)
        self.assertIn("blue:blue-team2", names)
        scrim.record_blue_agents(args, run_dir)  # resume re-records
        again = ao.load_manifest(test_dir)
        self.assertEqual(again["agents"], man["agents"])
        self.assertEqual(again["paths"], man["paths"])


class TeardownCollection(TmpCase):
    """INV6/INV9: collect -> scrim-report -> ingest_verdict, all before `badauto destroy`."""

    def setUp(self):
        super().setUp()
        self.args = make_args(self.comp, run_dir=None, competition="demo")
        self.run_dir, self.test_dir = self.resolve(self.args)
        self.args.run_dir = str(self.run_dir)  # what main() sets before teardown
        self.calls = []
        self.warnings = []

    def _label(self, cmd):
        text = " ".join(str(c) for c in cmd)
        if "scrim-report.py" in text:
            return "scrim-report"
        if "badauto" in text:
            return "badauto destroy"
        if "destroy-competition.py" in text:
            return "destroy-competition"
        return text

    def _fake_run(self, cmd, **kwargs):
        self.calls.append(self._label(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _patches(self, *, collect=None, ingest=None, run=None, log=True):
        collect = collect or (lambda *a, **k: self.calls.append("collect") or {"summary": {}})
        ingest = ingest or (lambda *a, **k: self.calls.append("ingest") or {})
        patches = [
            mock.patch.object(scrim, "api_key", lambda *a, **k: "k"),
            mock.patch.object(scrim, "run", side_effect=run or self._fake_run),
            mock.patch.object(ao, "collect", side_effect=collect),
            mock.patch.object(ao, "ingest_verdict", side_effect=ingest),
        ]
        if log:
            patches.append(mock.patch.object(scrim, "log",
                                             side_effect=self.warnings.append))
        return patches

    def _teardown(self, **kwargs):
        with contextlib.ExitStack() as stack:
            for patch in self._patches(**kwargs):
                stack.enter_context(patch)
            scrim.stage_teardown(self.args, {"ENGINE_IP": "10.0.0.193"})

    def test_the_collector_runs_before_badauto_destroy(self):
        self._teardown()
        self.assertEqual(self.calls, ["collect", "scrim-report", "ingest",
                                      "badauto destroy", "destroy-competition"])

    def test_badauto_destroy_still_runs_when_the_collector_raises(self):
        def boom(*a, **k):
            self.calls.append("collect")
            raise RuntimeError("scp: connect to host 10.0.0.199: No route to host")

        self._teardown(collect=boom)
        self.assertIn("badauto destroy", self.calls)
        self.assertIn("destroy-competition", self.calls)
        self.assertTrue(any("WARNING" in w and "collection" in w for w in self.warnings),
                        self.warnings)

    def test_ingest_is_reached_after_the_red_pull_and_a_report_crash_only_warns(self):
        def failing_run(cmd, **kwargs):
            label = self._label(cmd)
            self.calls.append(label)
            if label == "scrim-report":
                raise RuntimeError("scrim-report.py blew up")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        self._teardown(run=failing_run)
        self.assertLess(self.calls.index("collect"), self.calls.index("scrim-report"))
        self.assertLess(self.calls.index("scrim-report"), self.calls.index("ingest"))
        self.assertLess(self.calls.index("ingest"), self.calls.index("badauto destroy"))
        self.assertTrue(any("WARNING" in w and "scrim-report" in w for w in self.warnings),
                        self.warnings)

    def test_a_failing_scrim_report_exit_code_only_warns(self):
        def rc1_run(cmd, **kwargs):
            label = self._label(cmd)
            self.calls.append(label)
            # only the report fails: a failing badauto destroy raises (fail-loud
            # teardown, scale8-hardening), and this gate is about the report's rc
            if label == "scrim-report":
                return subprocess.CompletedProcess(cmd, 1, "", "no such run dir")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        self._teardown(run=rc1_run)
        self.assertIn("ingest", self.calls)
        self.assertIn("badauto destroy", self.calls)
        self.assertTrue(any("WARNING" in w and "exited 1" in w for w in self.warnings),
                        self.warnings)

    def test_interaction_md_reaches_the_verdict_ingest_reads(self):
        """The bridge that makes the ordering useful: scrim-report writes <run_dir>/, ingest
        reads <test_dir>/evidence/harness/ — the same place the collector would have put it."""
        (self.run_dir / "INTERACTION.md").write_text(
            "# INTERACTION — demo\n\n## Verdict\n\n"
            "**GREEN** — interaction score 11; gates 9 pass / 1 fail / 3 n/a\n")
        # the REAL ingest_verdict, so this fails if the report never reaches its source path
        with mock.patch.object(scrim, "api_key", lambda *a, **k: "k"), \
                mock.patch.object(scrim, "log", side_effect=self.warnings.append), \
                mock.patch.object(scrim, "run", side_effect=self._fake_run), \
                mock.patch.object(ao, "collect",
                                  side_effect=lambda *a, **k: self.calls.append("collect") or {}):
            scrim.stage_teardown(self.args, {"ENGINE_IP": "10.0.0.193"})
        verdict = ao.load_manifest(self.test_dir).get("verdict") or {}
        self.assertEqual(verdict.get("status"), "GREEN")
        self.assertEqual(verdict.get("score"), 11)
        self.assertEqual(verdict.get("source"), "evidence/harness/INTERACTION.md")
        self.assertTrue((self.test_dir / "evidence" / "harness" / "INTERACTION.md").exists())
        self.assertIn("badauto destroy", self.calls)


class Finalize(TmpCase):
    """INV6: finalize is the last step and runs even when --keep-range skipped the destroy."""

    def setUp(self):
        super().setUp()
        self.args = make_args(self.comp, run_dir=None, competition="demo")
        self.run_dir, self.test_dir = self.resolve(self.args)
        self.calls = []

    def _fake_run(self, cmd, **kwargs):
        text = " ".join(str(c) for c in cmd)
        self.calls.append("destroy-competition" if "destroy-competition.py" in text
                          else ("badauto destroy" if "badauto" in text else text))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def test_finalize_runs_last_on_the_keep_range_path(self):
        self.args.keep_range = True
        with mock.patch.object(scrim, "stage_run", lambda *a: self.calls.append("stage_run")), \
                mock.patch.object(scrim, "stage_capture",
                                  lambda *a: self.calls.append("capture")), \
                mock.patch.object(scrim, "api_key", lambda *a, **k: "k"), \
                mock.patch.object(scrim, "log", lambda *a, **k: None), \
                mock.patch.object(scrim, "run", side_effect=self._fake_run), \
                mock.patch.object(ao, "collect",
                                  side_effect=lambda *a, **k: self.calls.append("collect") or {}), \
                mock.patch.object(ao, "ingest_verdict",
                                  side_effect=lambda *a, **k: self.calls.append("ingest") or {}), \
                mock.patch.object(ao, "finalize",
                                  side_effect=lambda *a, **k: self.calls.append("finalize")
                                  or {"warnings": []}):
            failure = scrim.run_event_and_finish(self.args, {"ENGINE_IP": "x"}, 0.0)
        self.assertIsNone(failure)
        self.assertEqual(self.calls[-1], "finalize")
        self.assertIn("badauto destroy", self.calls)
        self.assertNotIn("destroy-competition", self.calls)  # --keep-range kept the range
        self.assertLess(self.calls.index("badauto destroy"), self.calls.index("finalize"))

    def test_finalize_failure_only_warns(self):
        warnings = []
        with mock.patch.object(scrim, "stage_run", lambda *a: None), \
                mock.patch.object(scrim, "stage_capture", lambda *a: None), \
                mock.patch.object(scrim, "stage_teardown", lambda *a, **k: None), \
                mock.patch.object(scrim, "log", side_effect=warnings.append), \
                mock.patch.object(ao, "finalize",
                                  side_effect=RuntimeError("no space left on device")):
            failure = scrim.run_event_and_finish(self.args, {"ENGINE_IP": "x"}, 0.0)
        self.assertIsNone(failure)
        self.assertTrue(any("WARNING" in w and "finalize" in w for w in warnings), warnings)


class CaptureRegressions(TmpCase):
    """INV7/INV8: what stage_capture actually copies."""

    def test_blue_sub_md_is_captured(self):
        run_dir = self.root / "run"
        workdir = run_dir / "blue-team1"
        workdir.mkdir(parents=True)
        (workdir / "sub.md").write_text("## deliverable\n")       # what the prompt asks for
        (workdir / "sub.txt").write_text("notes\n")
        (workdir / "deliverable-notes.md").write_text("not a submission\n")
        (workdir / "LOG.md").write_text("# log\n")
        (run_dir / "blue-team2").mkdir()                          # no files: skipped
        ev = run_dir / "evidence"
        ev.mkdir()
        written = scrim.capture_blue_evidence(run_dir, ev, 2)
        self.assertTrue((ev / "blue-team1" / "sub.md").exists(),
                        "sub.md is the name the cycle prompt tells blue to write")
        self.assertTrue((ev / "blue-team1" / "sub.txt").exists())
        self.assertTrue((ev / "blue-team1" / "LOG.md").exists())
        self.assertFalse((ev / "blue-team1" / "deliverable-notes.md").exists())
        self.assertTrue(written)

    def test_blue_report_md_is_captured_when_blue_writes_it(self):
        run_dir = self.root / "run"
        workdir = run_dir / "blue-team1"
        workdir.mkdir(parents=True)
        (workdir / "REPORT.md").write_text("# after-action\n")
        ev = run_dir / "evidence"
        ev.mkdir()
        scrim.capture_blue_evidence(run_dir, ev, 1)
        self.assertTrue((ev / "blue-team1" / "REPORT.md").exists())

    def test_engine_capture_is_copied_not_moved(self):
        ev = self.root / "evidence"
        ev.mkdir()
        (ev / "final-scoreboard.json").write_text('{"teams": []}')
        (ev / "scoreboard-state.jsonl").write_text("{}\n")
        (ev / "final-services-team1.json").write_text("[]")
        written = scrim.capture_engine_evidence(ev)
        self.assertEqual(len(written), 3)
        self.assertTrue((ev / "engine" / "final-scoreboard.json").exists())
        self.assertTrue((ev / "engine" / "final-services-team1.json").exists())
        self.assertTrue((ev / "engine" / "scoreboard-state.jsonl").exists())
        # NOT moved: scrim-report.py reads this exact path for the scoreboard section
        self.assertTrue((ev / "final-scoreboard.json").exists())
        self.assertIn('"evidence" / "final-scoreboard.json"',
                      (_REPO / "scrim-report.py").read_text())


class BluePrompt(TmpCase):
    """INV10: blue is told to write the REPORT.md the collector looks for."""

    def setUp(self):
        super().setUp()
        (self.comp / "box_services.json").write_text(json.dumps({"web01": ["nginx"]}))
        (self.comp / "packet.md").write_text("# packet\n")
        self.run_dir = self.root / "run"
        (self.run_dir / "blue-team1").mkdir(parents=True)
        self.creds = {"ENGINE_IP": "10.0.0.193", "TEAM1_ID": "101", "TEAM1_PW": "p",
                      "BOX_PW": "p", "INJECT_PW": "p", "KEY_PATH": "/tmp/k",
                      "VM_USER": "u", "BOX_USER": "airship"}

    def test_the_cycle_prompt_asks_for_report_md(self):
        args = SimpleNamespace(competition="demo", run_dir=str(self.run_dir), teams=1,
                               duration_min=90, reasoning_effort="minimal")
        with mock.patch.object(scrim, "REPO", self.root):
            prompt = scrim.blue_cycle_prompt(1, self.creds, args, 600, 30, "", "", "", "")
        self.assertIn("REPORT.md", prompt)
        self.assertIn("workdir", prompt)
        self.assertIn("what you would change about this competition", prompt)
        # the change is additive: the cycle prompt's structure is untouched
        self.assertIn("CYCLE TASK", prompt)
        self.assertIn("ROE: never attack the engine", prompt)

    def test_stage_blues_asks_for_it_and_records_blue_identity(self):
        args = SimpleNamespace(competition="demo", teams=1, run_dir=str(self.run_dir),
                               blue_base_url="https://openrouter.ai/api/v1",
                               blue_model="m", reasoning_effort="minimal")
        _run_dir, test_dir = self.resolve(args)  # main() resolves before stage_blues
        with mock.patch.object(scrim, "REPO", self.root), \
                mock.patch.object(scrim, "api_key", lambda *a, **k: "k"):
            scrim.stage_blues(args, self.comp, self.run_dir, self.creds, 0.0)
        workdir = self.run_dir / "blue-team1"
        self.assertIn("REPORT.md", (workdir / "LOG.md").read_text())
        manifest = ao.load_manifest(test_dir)
        self.assertEqual(manifest["agents"]["blue"], {"present": True, "teams": 1})
        self.assertEqual(manifest["paths"]["blue_workdirs"], [str(workdir)])
        self.assertEqual(manifest["paths"]["engine_evidence"],
                         str(self.run_dir / "evidence" / "engine"))


if __name__ == "__main__":
    unittest.main()
