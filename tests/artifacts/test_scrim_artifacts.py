"""The scrim harness writes into its run's per-run test folder (artifacts_lib/artifacts_ops.py)."""

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

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from artifacts_lib import artifacts_ops as ao
from estate import state as estate_state


def _load(name, filename):
# tests/teardown tests' pattern: the harness filename has hyphens.
    spec = importlib.util.spec_from_file_location(name, _REPO / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


from scrim import fire_test, quotient_api, blue_agent, blue_prompt, core, endpoints, event_run, evidence, procs, red_link, red_stage, run_manifest, teardown_stage, test_folder

RUN_ID = "run-1a2b3c4d"
BOXES = [{"name": "dc01", "last_octet": 2, "template": "base-windows-server"},
         {"name": "web01", "last_octet": 4, "template": "base-ubuntu24.04-fix"}]


def make_comp(root, *, name="demo", run_id=RUN_ID):
    comp = Path(root) / "competitions" / name
    comp.mkdir(parents=True)
    (comp / "Compfile").write_text(f"name {name}-fixture\nscenario x\n")
    (comp / "boxes.json").write_text(json.dumps(BOXES))
    estate_state.write_flat(comp, {"run_id": run_id} if run_id else {})
    return comp


def make_args(comp, **over):
    args = SimpleNamespace(competition=Path(comp).name, comp_dir=str(comp), teams=2,
                           duration_min=90, keep_range=False, keep_templates=False,
                           blue_watchdog=False, run_dir=None,
                           red_ip="10.0.0.198", red_vmid=None)
    for key, value in over.items():
        setattr(args, key, value)
    return args


class TmpCase(unittest.TestCase):
    def setUp(self):
        # Pin `_red_vm_still_exists`: it asks the real cluster, and a live red01 made
        # teardown assert "STILL PRESENT" in tests that are about ordering.
        #
        # enterContext(), not addCleanup(patcher.start): the latter registered `patcher.start`
        # as the cleanup, so every setUp applied a patch nothing ever stopped.
        self.enterContext(mock.patch.object(teardown_stage, "_red_vm_still_exists",
                                            lambda *a, **k: False))
        # These tests must not dial red01 (a real ssh per case turned a 1s file into 170s).
        self.enterContext(mock.patch.object(red_link, "pull_red_state", lambda *a, **k: []))
        self.enterContext(mock.patch.object(red_link, "start_red_director",
                                            lambda *a, **k: True))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.comp = make_comp(self.root)

    def resolve(self, args, comp=None):
        with mock.patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "pve",
                                          "TEZ_PROXMOX_ENDPOINT": "https://203.0.113.20:8006"}):
            return test_folder.resolve_run_dir(comp or self.comp, args)


class RunDirResolution(TmpCase):
# INV1-INV3: where the run writes, and where its test folder lives.

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
        targets = ao.plan_targets(ao.load_manifest(test_dir), comp_dir=self.comp)
        harness = next(t for t in targets if t["name"] == "harness")
        self.assertEqual(harness.get("root"), str(test_dir))
        self.assertNotIn("status", harness)


    def test_untagged_comp_gets_one_stable_key(self):
    # INV1: the untagged key is stable per deploy AND per run — a resume returns to its own
    # folder, a second fresh run gets its own, so one run's evidence never sits beside another's.
        comp = make_comp(self.root, name="legacy", run_id=None)
        first_run, first_test = self.resolve(make_args(comp), comp=comp)
        self.assertTrue(first_test.name.startswith("untagged-"))
        self.assertEqual(first_run, first_test)
        resumed = self.resolve(make_args(comp, resume_event=True), comp=comp)
        self.assertEqual(resumed[0], first_run)
        self.assertEqual(resumed[1], first_test)
        second_run, second_test = self.resolve(make_args(comp), comp=comp)
        self.assertNotEqual(second_test, first_test)
        self.assertTrue(second_test.name.endswith("-run2"))
        folders = sorted(p.name for p in (comp / ao.DIRNAME).iterdir() if p.is_dir())
        self.assertEqual(folders, [first_test.name, second_test.name])

    def test_resume_resolves_to_the_same_folder(self):
        first_run, first_test = self.resolve(make_args(self.comp))
        (first_run / "T0.txt").write_text(json.dumps({"t0": 1000.0, "duration_min": 90}))
        (first_run / "run.json").write_text(json.dumps({"phase": "event", "t0": 1000.0}))
        second_run, second_test = self.resolve(make_args(self.comp, resume_event=True))
        self.assertEqual(second_run, first_run)
        self.assertEqual(second_test, first_test)
        folders = sorted(p.name for p in (self.comp / ao.DIRNAME).iterdir() if p.is_dir())
        self.assertEqual(folders, [RUN_ID], "a resume must not mint a second key")
        third_run, third_test = self.resolve(make_args(self.comp))
        self.assertNotEqual(third_test, first_test)
        self.assertEqual(third_run, third_test)
        self.assertTrue(third_test.name.endswith("-run2"))

    def test_recorded_run_dir_is_authoritative_for_the_resume(self):
        elsewhere = self.root / "elsewhere-run"
        elsewhere.mkdir()
        first = self.resolve(make_args(self.comp, run_dir=str(elsewhere)))
        self.assertEqual(first[0], elsewhere)
        second = self.resolve(make_args(self.comp, run_dir=None, resume_event=True))
        self.assertEqual(second[0], elsewhere, "the recorded paths.run_dir is authoritative")
        self.assertEqual(second[1], first[1])
        folders = sorted(p.name for p in (self.comp / ao.DIRNAME).iterdir() if p.is_dir())
        self.assertEqual(folders, [RUN_ID])



class PhaseMirror(TmpCase):
# INV4: test.json mirrors the phase markers; run.json stays the harness's own file.

    def test_record_phase_mirrors_into_test_json_and_keeps_run_json(self):
        args = make_args(self.comp, keep_range=True)
        run_dir, test_dir = self.resolve(args)
        run_manifest.record_phase(run_dir, args, "stage_red")
        man = run_manifest.load_manifest(run_dir)
        self.assertEqual(man["phase"], "stage_red")
        self.assertIsNone(man["t0"])
        self.assertTrue(man["keep_range"])
        self.assertEqual(man["competition"], "demo")
        self.assertEqual(man["teams"], 2)
        self.assertEqual(man["duration_min"], 90)
        self.assertEqual(os.stat(run_dir / run_manifest.RUN_MANIFEST).st_mode & 0o777, 0o600)
        mirrored = ao.load_manifest(test_dir)
        self.assertEqual([p["phase"] for p in mirrored["phases"]], ["stage_red"])
        self.assertEqual(mirrored["phase"], "stage_red")
        run_manifest.record_phase(run_dir, args, "event", t0=1234.5)
        self.assertEqual(run_manifest.load_manifest(run_dir)["t0"], 1234.5)
        self.assertEqual([p["phase"] for p in ao.load_manifest(test_dir)["phases"]],
                         ["stage_red", "event"])
        self.assertEqual(ao.load_manifest(test_dir)["phases"][1]["t0"], 1234.5)



class KeepTemplates(TmpCase):
# INV11: --keep-templates reaches run_terminal_teardown as full=False and survives a resume —
# otherwise the next run silently rebuilds, or destroys, the artifacts the practice verified.

    def _comp_with_lineup(self):
        (self.comp / "teams.json").write_text(json.dumps({"team1": {"identifier": 1}}))
        return self.comp

    def _range_teardown(self, args):
        from teardown import runner as teardown_runner
        with mock.patch.object(teardown_runner, "run_terminal_teardown",
                               return_value=mock.Mock(clean=True)) as destroy:
            teardown_stage.run_range_teardown(args, evidence_done=False)
        return destroy.call_args

    def test_keep_templates_tears_down_the_range_without_the_templates(self):
        comp = self._comp_with_lineup()
        call = self._range_teardown(make_args(comp, keep_templates=True))
        self.assertEqual(call.args[0], comp)
        self.assertFalse(call.kwargs["full"])

    def test_default_teardown_is_still_full(self):
        comp = self._comp_with_lineup()
        self.assertTrue(self._range_teardown(make_args(comp)).kwargs["full"])
    def test_resume_keeps_the_intent_the_first_run_had(self):
        args = make_args(self.comp)
        run_manifest.resume_intent(args, {"keep_templates": True})
        self.assertTrue(args.keep_templates)
        run_manifest.record_phase(self.root, args, "teardown")
        self.assertTrue(run_manifest.load_manifest(self.root)["keep_templates"])


class EvidenceDoneFollowsTheCollection(TmpCase):
# A collection that never produced a record must not journal the artifact step done: the destroy
# that follows makes a second attempt impossible. red01's absence proof is journalled either way.

    def _stage(self, collection):
        args = make_args(self.comp)
        args.test_dir = str(self.comp / ".automated-tests" / RUN_ID)
        with mock.patch.object(teardown_stage.test_folder, "collect_run_artifacts",
                                  return_value=collection), \
                mock.patch.object(teardown_stage.test_folder, "write_interaction_report"), \
                mock.patch.object(teardown_stage, "teardown_red", return_value=999), \
                mock.patch.object(teardown_stage, "run_range_teardown") as run:
            teardown_stage.stage_teardown(args, creds={"user": "sysadmin"})
        return run.call_args

    def test_a_failed_collection_leaves_the_artifacts_step_to_the_plan(self):
        self.assertFalse(self._stage(None).kwargs["evidence_done"])


    def test_a_failed_collection_still_journals_the_red_proof(self):
        (self.comp / "teams.json").write_text(json.dumps({"team1": {"identifier": 1}}))
        from teardown import runner as teardown_runner
        with mock.patch.object(teardown_runner, "record_evidence_done") as rec, \
                mock.patch.object(teardown_runner, "run_terminal_teardown",
                                  return_value=mock.Mock(clean=True)):
            teardown_stage.run_range_teardown(make_args(self.comp), evidence_done=False,
                                             red_vmid=999)
        self.assertIsNone(rec.call_args.kwargs["test_dir"])
        self.assertEqual(rec.call_args.kwargs["red_vmid"], 999)


class AgentIdentity(TmpCase):
# INV5/INV9: identity is recorded from the one ssh helper, and merged, never replaced.

    def setUp(self):
        super().setUp()
        # A fake repo root so the ssh helper reads THIS run's files without the real repo,
        # the sibling bad-auto, or the network.
        self.repo = self.root
        from estate import state as estate_state
        estate_state.write_flat(self.comp, {"red": {"vmid": 999, "ip": "10.0.0.199"}})
        (self.comp / "credentials.txt").write_text("engine http://203.0.113.20:8006/\n")
        (self.comp / "teams.json").write_text(
            json.dumps({"team1": {"identifier": "130", "password": "pw"}}))
        for patch in (mock.patch.object(core, "REPO", self.repo),
                      mock.patch.dict(os.environ, {"TEZ_ENGINE_USERNAME": "sysadmin",
                                                   "TEZ_PROXMOX_NODE": "pve"})):
            patch.start()
            self.addCleanup(patch.stop)

    def test_red_ssh_spec_is_the_one_helper_red_ssh_ctx_uses(self):
        args = make_args(self.comp, red_ip="10.0.0.198")  # the CLI wins over the record
        target, common, jump = red_link._red_ssh_ctx(args)
        spec, spec_common = red_link._red_ssh_spec(args)
        self.assertEqual(spec["host"], "10.0.0.198")
        self.assertEqual(spec["user"], "sysadmin")
        self.assertEqual(spec["key"], str(self.repo / "proxmox"))
        self.assertEqual(spec["jump"], jump)
        self.assertIn("ProxyCommand=ssh", jump)
        self.assertIn("203.0.113.20", jump)
        self.assertEqual(target, "sysadmin@10.0.0.198")
        self.assertEqual(common, spec_common)
        self.assertIn(spec["key"], common)

    def test_stage_red_records_the_identity_in_the_manifest(self):
        args = make_args(self.comp, run_dir=None, red_ip="10.0.0.199", red_vmid=999,
                         llm_base_url="https://openrouter.ai/api/v1", red_model="m",
                         reasoning_effort=None, red_tunnel="off")
        run_dir, test_dir = self.resolve(args)
        gate_out = ("  red_identity      : PASS  10.200.0.11 visible end-to-end\n"
                    "  red_teams         : PASS  red01 reached all 2 teams\n")
        with mock.patch.object(procs, "run",
                               lambda *a, **k: subprocess.CompletedProcess(
                                   a[0], 0, gate_out, "")), \
                mock.patch.object(red_link, "check_red_llm", lambda *a, **k: True), \
                mock.patch.object(endpoints, "api_key", lambda *a, **k: "k"), \
                mock.patch("red.tlaloc_ops.plant_assume_breach",
                                  lambda *a, **k: {"ok": True, "red_ip": "10.0.0.199"}), \
                mock.patch.object(red_stage.core, "read_state",
                                  lambda *a, **k: {"run_id": "run-1a2b3c4d",
                                                   "scoring_vm_id": 1300,
                                                   "engine_mgmt_ip": "203.0.113.20"}), \
                mock.patch.object(red_stage, "verify_tlaloc_seeded",
                                  lambda *a, **k: ["192.168.101.2", "192.168.102.2"]), \
                mock.patch("scrim.clean_trails.clean_trails", lambda *a, **k: None), \
                mock.patch.object(red_link, "start_red_director", lambda *a, **k: None):
            # creds must be real-shaped: the pre-T0 reachability gate reads ENGINE_IP/ADMIN_PW
            red_stage.stage_red(args, self.comp, {"ENGINE_IP": "203.0.113.20",
                                              "ADMIN_PW": "pw"}, run_dir)
        red = ao.load_manifest(test_dir)["agents"]["red"]
        self.assertTrue(red["present"])
        self.assertEqual(red["ip"], "10.0.0.199")
        self.assertEqual(red["vmid"], 999)
        self.assertEqual(red["node"], "pve")
        spec, _common = red_link._red_ssh_spec(args)
        self.assertEqual(red["ssh"], spec)
        # the collector's red target dials EXACTLY this spec, not config.yaml
        targets = ao.plan_targets(ao.load_manifest(test_dir), comp_dir=self.comp)
        red_target = next(t for t in targets if t["name"] == "red01")
        self.assertEqual(red_target["ssh"], spec)
        self.assertEqual(red_target["vmid"], 999)
        test_folder.record_red_agent(args)  # a resume re-records; identical, not erased
        self.assertEqual(ao.load_manifest(test_dir)["agents"]["red"], red)



class TeardownCollection(TmpCase):
# INV6/INV9: collect -> scrim-report -> ingest_verdict, all before `red destroy`.

    def setUp(self):
        super().setUp()
        self.args = make_args(self.comp, run_dir=None, competition="demo")
        self.run_dir, self.test_dir = self.resolve(self.args)
        self.args.run_dir = str(self.run_dir)
        self.calls = []
        self.warnings = []

    def _label(self, cmd):
        text = " ".join(str(c) for c in cmd)
        if "scrim-report.py" in text:
            return "scrim-report"
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
            mock.patch.object(endpoints, "api_key", lambda *a, **k: "k"),
            mock.patch.object(procs, "run", side_effect=run or self._fake_run),
            mock.patch.object(ao, "collect", side_effect=collect),
            mock.patch.object(ao, "ingest_verdict", side_effect=ingest),
            # the range teardown is in-process now: the label records the seam
            mock.patch.object(teardown_stage, "run_range_teardown",
                              side_effect=lambda *a, **k: self.calls.append("destroy-competition")),
            # this scenario is a run that DID stage red (a red01 exists to destroy)
            mock.patch.object(teardown_stage, "_manifest_red_vmid", lambda *a, **k: 999),
            mock.patch("red.tlaloc_ops.destroy_tlaloc_red",
                       side_effect=lambda *a, **k: self.calls.append("red destroy") or
                       {"ok": True}),
        ]
        if log:
            patches.append(mock.patch.object(test_folder, "log",
                                             side_effect=self.warnings.append))
        return patches

    def _teardown(self, **kwargs):
        with contextlib.ExitStack() as stack:
            for patch in self._patches(**kwargs):
                stack.enter_context(patch)
            teardown_stage.stage_teardown(self.args, {"ENGINE_IP": "203.0.113.20"})

    def test_the_collector_runs_before_badauto_destroy(self):
        self._teardown()
        self.assertEqual(self.calls, ["collect", "scrim-report", "ingest",
                                      "red destroy", "destroy-competition"])

    def test_badauto_destroy_still_runs_when_the_collector_raises(self):
        def boom(*a, **k):
            self.calls.append("collect")
            raise RuntimeError("scp: connect to host 10.0.0.199: No route to host")

        self._teardown(collect=boom)
        self.assertIn("red destroy", self.calls)
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
        self.assertLess(self.calls.index("ingest"), self.calls.index("red destroy"))
        self.assertTrue(any("WARNING" in w and "scrim-report" in w for w in self.warnings),
                        self.warnings)


    def test_interaction_md_reaches_the_verdict_ingest_reads(self):
    # The bridge that makes the ordering useful: scrim-report writes <run_dir>/, ingest reads
    # <test_dir>/evidence/harness/ — the same place the collector would have put it.
        (self.run_dir / "INTERACTION.md").write_text(
            "# INTERACTION — demo\n\n## Verdict\n\n"
            "**GREEN** — interaction score 11; gates 9 pass / 1 fail / 3 n/a\n")
        # the REAL ingest_verdict, so this fails if the report never reaches its source path
        with mock.patch.object(endpoints, "api_key", lambda *a, **k: "k"), \
                mock.patch.object(test_folder, "log", side_effect=self.warnings.append), \
                mock.patch.object(teardown_stage, "_manifest_red_vmid", lambda *a, **k: 999), \
                mock.patch("red.tlaloc_ops.destroy_tlaloc_red",
                           side_effect=lambda *a, **k: self.calls.append("red destroy") or
                           {"ok": True}), \
                mock.patch.object(procs, "run", side_effect=self._fake_run), \
                mock.patch.object(teardown_stage, "run_range_teardown",
                                  side_effect=lambda *a, **k: self.calls.append("destroy-competition")), \
                mock.patch.object(ao, "collect",
                                  side_effect=lambda *a, **k: self.calls.append("collect") or {}):
            teardown_stage.stage_teardown(self.args, {"ENGINE_IP": "203.0.113.20"})
        verdict = ao.load_manifest(self.test_dir).get("verdict") or {}
        self.assertEqual(verdict.get("status"), "GREEN")
        self.assertEqual(verdict.get("score"), 11)
        self.assertEqual(verdict.get("source"), "evidence/harness/INTERACTION.md")
        self.assertTrue((self.test_dir / "evidence" / "harness" / "INTERACTION.md").exists())
        self.assertIn("red destroy", self.calls)


class Finalize(TmpCase):
# INV6: finalize is the last step and runs even when --keep-range skipped the destroy.

    def setUp(self):
        super().setUp()
        self.args = make_args(self.comp, run_dir=None, competition="demo")
        self.run_dir, self.test_dir = self.resolve(self.args)
        self.args.run_dir = str(self.run_dir)
        self.calls = []

    def _fake_run(self, cmd, **kwargs):
        text = " ".join(str(c) for c in cmd)
        self.calls.append("destroy-competition" if "destroy-competition.py" in text
                          else ("red destroy" if "badauto" in text else text))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def test_finalize_runs_last_on_the_keep_range_path(self):
        # this scenario is a run that DID stage red (a red01 exists to destroy)
        self.args.keep_range = True
        with mock.patch.object(event_run, "stage_run", lambda *a: self.calls.append("stage_run")), \
                mock.patch.object(evidence, "stage_capture",
                                  lambda *a: self.calls.append("capture")), \
                mock.patch.object(endpoints, "api_key", lambda *a, **k: "k"), \
                mock.patch.object(test_folder, "log", lambda *a, **k: None), \
                mock.patch.object(teardown_stage, "_manifest_red_vmid", lambda *a, **k: 999), \
                mock.patch("red.tlaloc_ops.destroy_tlaloc_red",
                           side_effect=lambda *a, **k: self.calls.append("red destroy") or
                           {"ok": True}), \
                mock.patch.object(procs, "run", side_effect=self._fake_run), \
                mock.patch.object(ao, "collect",
                                  side_effect=lambda *a, **k: self.calls.append("collect") or {}), \
                mock.patch.object(ao, "ingest_verdict",
                                  side_effect=lambda *a, **k: self.calls.append("ingest") or {}), \
                mock.patch.object(ao, "finalize",
                                  side_effect=lambda *a, **k: self.calls.append("finalize")
                                  or {"warnings": []}):
            failure = event_run.run_event_and_finish(self.args, {"ENGINE_IP": "x"}, 0.0)
        self.assertIsNone(failure)
        self.assertEqual(self.calls[-1], "finalize")
        self.assertIn("red destroy", self.calls)
        self.assertNotIn("destroy-competition", self.calls)
        self.assertLess(self.calls.index("red destroy"), self.calls.index("finalize"))



class CaptureRegressions(TmpCase):
# INV7/INV8: what stage_capture actually copies.

    def test_blue_sub_md_is_captured(self):
        run_dir = self.root / "run"
        workdir = run_dir / "blue-team1"
        workdir.mkdir(parents=True)
        (workdir / "sub.md").write_text("## deliverable\n")
        (workdir / "sub.txt").write_text("notes\n")
        (workdir / "deliverable-notes.md").write_text("not a submission\n")
        (workdir / "LOG.md").write_text("# log\n")
        (run_dir / "blue-team2").mkdir()
        ev = run_dir / "evidence"
        ev.mkdir()
        written = evidence.capture_blue_evidence(run_dir, ev, 2)
        self.assertTrue((ev / "blue-team1" / "sub.md").exists(),
                        "sub.md is the name the cycle prompt tells blue to write")
        self.assertTrue((ev / "blue-team1" / "sub.txt").exists())
        self.assertTrue((ev / "blue-team1" / "LOG.md").exists())
        self.assertFalse((ev / "blue-team1" / "deliverable-notes.md").exists())
        self.assertTrue(written)


    def test_engine_capture_is_copied_not_moved(self):
        ev = self.root / "evidence"
        ev.mkdir()
        (ev / "final-scoreboard.json").write_text('{"teams": []}')
        (ev / "scoreboard-state.jsonl").write_text("{}\n")
        (ev / "final-services-team1.json").write_text("[]")
        written = evidence.capture_engine_evidence(ev)
        self.assertEqual(len(written), 3)
        self.assertTrue((ev / "engine" / "final-scoreboard.json").exists())
        self.assertTrue((ev / "engine" / "final-services-team1.json").exists())
        self.assertTrue((ev / "engine" / "scoreboard-state.jsonl").exists())
        # NOT moved: scrim-report.py reads this exact path for the scoreboard section
        self.assertTrue((ev / "final-scoreboard.json").exists())


class BluePrompt(TmpCase):
# INV10: blue is told to write the REPORT.md the collector looks for.

    def setUp(self):
        super().setUp()
        (self.comp / "box_services.json").write_text(json.dumps({"web01": ["nginx"]}))
        (self.comp / "packet.md").write_text("# packet\n")
        self.run_dir = self.root / "run"
        (self.run_dir / "blue-team1").mkdir(parents=True)
        self.creds = {"ENGINE_IP": "203.0.113.20", "TEAM1_ID": "101", "TEAM1_PW": "p",
                      "BOX_PW": "p", "INJECT_PW": "p", "KEY_PATH": "/tmp/k",
                      "VM_USER": "u", "BOX_USER": "airship"}


    def test_stage_blues_asks_for_it_and_records_blue_identity(self):
        args = SimpleNamespace(competition="demo", teams=1, run_dir=str(self.run_dir),
                               blue_base_url="https://openrouter.ai/api/v1",
                               blue_model="m", reasoning_effort="minimal")
        _run_dir, test_dir = self.resolve(args)
        with mock.patch.object(core, "REPO", self.root), \
                mock.patch.object(endpoints, "api_key", lambda *a, **k: "k"):
            blue_agent.stage_blues(args, self.comp, self.run_dir, self.creds, 0.0)
        workdir = self.run_dir / "blue-team1"
        self.assertIn("REPORT.md", (workdir / "LOG.md").read_text())
        manifest = ao.load_manifest(test_dir)
        self.assertEqual(manifest["agents"]["blue"], {"present": True, "teams": 1})
        self.assertEqual(manifest["paths"]["blue_workdirs"], [str(workdir)])
        self.assertEqual(manifest["paths"]["engine_evidence"],
                         str(self.run_dir / "evidence" / "engine"))


class FireTest(TmpCase):
# The fire test, with the box and the scoreboard scripted (no SSH, no engine): pins the residue
# guard (an aborted earlier run can leave the unit down), the watched row (web01-<display>, not
# "first web01*"), and agreement polling (probe and scoreboard agree, not one stale sample).

    def setUp(self):
        super().setUp()
        (self.comp / "box_services.json").write_text(json.dumps({"web01": ["ssh", "nginx"]}))
        doc = estate_state.EstateState.load(self.comp)
        doc.section("roster").update({"targets": {"team1-web01": {"ip": "192.168.130.4"}}})
        doc.save()
        self.creds = {"ENGINE_IP": "10.0.0.250", "TEAM1_ID": "130", "TEAM1_PW": "p",
                      "BOX_PW": "p", "INJECT_PW": "p", "KEY_PATH": "/tmp/k",
                      "VM_USER": "u", "BOX_USER": "medic"}
        self.args = SimpleNamespace(competition="demo", teams=1, duration_min=90)
        self.sudo_calls = []
        self.logs = []

    def _ssh(self, states):
    # ssh_web01 scripted by command content; `states` is the is-active answer queue.
        def runner(cmd, timeout=60):
            if "is-active" in cmd:
                return SimpleNamespace(returncode=0,
                                       stdout=(states.pop(0) if states else "inactive") + "\n",
                                       stderr="")
            if "list-unit-files" in cmd:
                return SimpleNamespace(returncode=0, stdout="nginx\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return runner

    def _sudo(self):
        def runner(script, timeout=120):
            self.sudo_calls.append(script)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return runner

    def _scoreboard(self, downs):
    # one row-set per poll: `downs` says whether web01-http is down on each successive poll
        seq = [[{"service": "web01-ssh", "up": True},
                {"service": "web01-http", "up": not d}] for d in downs]

        def parsed(creds, team):
            return seq.pop(0) if seq else [{"service": "web01-ssh", "up": True},
                                           {"service": "web01-http", "up": True}]
        return parsed

    def _run(self, states, downs, codes):
    # Run the fire test on a FAKE clock: sleep is a no-op and each time.time() call advances 30s,
    # so the deadline loops terminate after a few polls.
        clock = {"t": 0.0}

        def fake_time():
            clock["t"] += 30
            return clock["t"]

        http_seq = list(codes)

        def fake_run_tree(cmd, **kw):
            return SimpleNamespace(returncode=0,
                                   stdout=(http_seq.pop(0) if http_seq else "000"),
                                   stderr="")

        with mock.patch.object(fire_test.time, "sleep", lambda s: None), \
                mock.patch.object(fire_test.time, "time", fake_time), \
                mock.patch.object(quotient_api, "parsed_status", self._scoreboard(downs)), \
                mock.patch.object(fire_test.procs, "run_tree", fake_run_tree), \
                mock.patch.object(fire_test, "log", side_effect=self.logs.append):
            fire_test.run_fire_test(self.args, self.comp, self.creds,
                                self._ssh(states), self._sudo())

    def test_green_path_stops_and_restores_the_scored_unit(self):
        self._run(["active"], [True], ["000", "200"])
        self.assertTrue(any("systemctl stop" in c for c in self.sudo_calls))
        self.assertTrue(any("systemctl start" in c for c in self.sudo_calls))
        self.assertEqual(self.args.web_unit, "nginx")
        self.assertTrue(any("down_detected=True restored=True healed=True" in l
                            for l in self.logs))

    def test_pre_existing_outage_is_restored_before_the_stop(self):
        self._run(["inactive", "active", "active"], [True], ["000", "200"])
        self.assertTrue(any("pre-existing outage" in l for l in self.logs))
        self.assertLess([i for i, c in enumerate(self.sudo_calls) if "start" in c][0],
                        [i for i, c in enumerate(self.sudo_calls) if "stop" in c][0])

    def test_unrestorable_outage_aborts_before_the_stop(self):
        with self.assertRaises(RuntimeError) as raised:
            # every is-active poll answers inactive; time.time advances 30s per call so the
            # guard's bounded loop terminates
            clock = {"t": 0.0}

            def fake_time():
                clock["t"] += 30
                return clock["t"]

            with mock.patch.object(fire_test.time, "sleep", lambda s: None), \
                    mock.patch.object(fire_test.time, "time", fake_time), \
                    mock.patch.object(fire_test, "log", side_effect=self.logs.append):
                fire_test.run_fire_test(self.args, self.comp, self.creds,
                                    self._ssh(["inactive"]), self._sudo())
        self.assertIn("pre-existing outage", str(raised.exception))
        self.assertFalse(any("systemctl stop" in c for c in self.sudo_calls),
                         "must not stop a unit it could not first bring up")


if __name__ == "__main__":
    unittest.main()
