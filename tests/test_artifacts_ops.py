"""Per-run test artifacts (artifacts_ops.py): the collector that runs at teardown.

Why this suite exists: the collector's whole job is to be *honest about absence*. An empty
artifact folder that could mean "the agent wrote nothing", "the pull failed", or "the VM was
already destroyed" is the failure mode this feature was built to remove, so most of these tests
are about which status a missing thing gets, and about a re-run not destroying what the first
run proved. Offline; the transport is injected (there is no estate access here).
"""

import json
import os
from types import SimpleNamespace
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import artifacts_ops as ao

RED_REPORT = "/var/lib/bad-auto/report-*.md"


def make_comp(root, *, run_id="run-1a2b3c4d", comp="demo"):
    comp = Path(root) / "competitions" / comp
    comp.mkdir(parents=True, exist_ok=True)
    (comp / "Compfile").write_text("name demo-2026-10-03\nscenario x\n")
    state = {"teams": {"team1": {"identifier": "101"}}}
    if run_id:
        state["run_id"] = run_id
    (comp / ".deploy_state.json").write_text(json.dumps(state))
    return comp


def fake_transport(files, calls=None, unreachable=False, journal=None):
    """A transport whose `scp-jump` route answers from `files` (glob keys allowed).

    `ssh-cmd` is faked too, and deliberately: a real one would dial red01 from a unit test. It
    answers `journal` when given, else reports the command as having produced nothing."""
    import fnmatch

    def _fake(ssh, remote, dest, timeout=120):
        if calls is not None:
            calls.append(remote)
        if unreachable:
            raise ao.Unreachable("host is down")
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        written = []
        for name, body in files.items():
            if fnmatch.fnmatch(name, remote) or name == remote:
                path = dest / Path(name).name
                path.write_text(body)
                written.append(path)
        if not written:
            raise FileNotFoundError(remote)
        return sorted(written)

    def _fake_cmd(ssh, cmd, dest_file, timeout=90):
        if calls is not None:
            calls.append(cmd)
        if unreachable:
            raise ao.Unreachable("host is down")
        if journal is None:
            raise FileNotFoundError(cmd)
        Path(dest_file).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_file).write_text(journal)
        return [Path(dest_file)]

    transport = dict(ao.default_transport())
    transport["scp-jump"] = _fake
    transport["ssh-cmd"] = _fake_cmd
    return transport


class TestKey(unittest.TestCase):
    def test_run_id_from_state_is_the_folder_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            self.assertEqual(ao.run_id_from_state(comp), "run-1a2b3c4d")
            self.assertEqual(ao.test_key(comp), "run-1a2b3c4d")

    def test_missing_run_id_falls_back_to_a_stable_untagged_key(self):
        """Every on-disk .deploy_state.json today predates run ids, and teardown is re-run
        until clean — a fresh timestamp per invocation would split one run's evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp, run_id=None)
            first = ao.test_key(comp)
            time.sleep(0.01)
            self.assertEqual(first, ao.test_key(comp))
            self.assertTrue(first.startswith("untagged-"), first)

    def test_untagged_key_changes_when_a_new_deploy_writes_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp, run_id=None)
            first = ao.test_key(comp)
            os.utime(comp / ".deploy_state.json", (time.time() + 120, time.time() + 120))
            self.assertNotEqual(first, ao.test_key(comp))

    def test_invalid_run_id_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp, run_id="not-a-run-id")
            self.assertEqual(ao.run_id_from_state(comp), "")
            self.assertTrue(ao.test_key(comp).startswith("untagged-"))


class TestEnsureTest(unittest.TestCase):
    def test_creates_layout_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            path, manifest = ao.ensure_test(comp, kind="scrim", script="run-agent-scrim.py",
                                            teams=2, boxes=["dc01"], node="pve")
            self.assertTrue((path / "evidence" / "red").is_dir())
            self.assertEqual(manifest["key"], "run-1a2b3c4d")
            self.assertEqual(manifest["run_id"], "run-1a2b3c4d")
            created = manifest["created_at"]
            path2, manifest2 = ao.ensure_test(comp, kind="scrim", teams=4)
            self.assertEqual(path, path2)
            self.assertEqual(manifest2["created_at"], created, "created_at must be sticky")
            self.assertEqual(manifest2["teams"], 2, "a later caller must not overwrite truth")

    def test_refuses_to_mix_two_runs_in_one_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            path, _ = ao.ensure_test(comp, kind="scrim", run_id="run-1a2b3c4d")
            with self.assertRaises(RuntimeError) as ctx:
                ao.ensure_test(comp, kind="scrim", run_id="run-deadbeef", key="run-1a2b3c4d")
            self.assertIn("refusing", str(ctx.exception))
            self.assertEqual(ao.load_manifest(path)["run_id"], "run-1a2b3c4d")

    def test_record_paths_merges_instead_of_replacing(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            path, _ = ao.ensure_test(comp, kind="scrim")
            ao.record_paths(path, run_dir="/a")
            ao.record_paths(path, engine_evidence="/b")
            self.assertEqual(ao.load_manifest(path)["paths"],
                             {"run_dir": "/a", "engine_evidence": "/b"})

    def test_record_paths_stores_absolute_paths(self):
        """The manifest is read from a different CWD than the one that wrote it (teardown runs
        from the repo root, the harness from its worktree), so a relative path here is a bug
        waiting for a reader in the wrong directory."""
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            path, _ = ao.ensure_test(comp, kind="scrim")
            os.chdir(tmp)
            try:
                recorded = ao.record_paths(path, run_dir="run", blue_workdirs=["run/blue-team1"])
            finally:
                os.chdir(_REPO)
            self.assertTrue(Path(recorded["run_dir"]).is_absolute(), recorded)
            self.assertEqual(recorded["run_dir"], str((Path(tmp) / "run").resolve()))
            self.assertTrue(Path(recorded["blue_workdirs"][0]).is_absolute())

    def test_a_relative_path_in_an_old_manifest_still_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            run = Path(tmp) / "run"
            (run / "evidence").mkdir(parents=True)
            (run / "run.json").write_text("{}")
            path, _ = ao.ensure_test(comp, kind="scrim")
            manifest = ao.load_manifest(path)
            manifest["paths"] = {"run_dir": "run"}  # as an older/hand-written manifest may hold
            ao.save_manifest(path, manifest)
            cwd = os.getcwd()
            os.chdir(tmp)  # the CWD the path was relative to
            try:
                by_name = {t["name"]: t for t in ao.plan_targets(ao.load_manifest(path),
                                                                 comp_dir=comp)}
            finally:
                os.chdir(cwd)
            self.assertNotIn("status", by_name["harness"],
                             "a run dir recorded relative to the writer's CWD must still be found")

    def test_record_phase_appends_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            path, _ = ao.ensure_test(comp, kind="scrim")
            ao.record_phase(path, "stage_red")
            ao.record_phase(path, "event", t0=123.0)
            manifest = ao.load_manifest(path)
            self.assertEqual([p["phase"] for p in manifest["phases"]], ["stage_red", "event"])
            self.assertEqual(manifest["phase"], "event")


class TestPlanTargets(unittest.TestCase):
    def test_no_agents_means_everything_is_skipped_with_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            path, _ = ao.ensure_test(comp, kind="deploy")
            targets = ao.plan_targets(ao.load_manifest(path), comp_dir=comp)
            by_name = {t["name"]: t for t in targets}
            self.assertEqual(by_name["red01"]["status"], ao.SKIPPED)
            self.assertEqual(by_name["blue"]["status"], ao.SKIPPED)
            self.assertIn("no red agent", by_name["red01"]["note"])

    def test_present_agents_produce_real_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            run = Path(tmp) / "run"
            blue = run / "blue-team1"
            blue.mkdir(parents=True)
            path, _ = ao.ensure_test(comp, kind="scrim")
            ao.update_manifest(path, agents={"red": {"present": True, "vmid": 999,
                                                     "node": "pve", "ssh": {"host": "10.0.0.198"}},
                                             "blue": {"present": True, "teams": 1}})
            ao.record_paths(path, run_dir=str(run), blue_workdirs=[str(blue)])
            by_name = {t["name"]: t for t in ao.plan_targets(ao.load_manifest(path),
                                                             comp_dir=comp)}
            red_want = by_name["red01"]["want"]
            self.assertIn(RED_REPORT, [w.get("remote") for w in red_want])
            self.assertIn("journalctl -u bad-auto",
                          " ".join(w.get("cmd") or "" for w in red_want))
            self.assertEqual(by_name["red01"]["ssh"]["host"], "10.0.0.198")
            self.assertEqual(by_name["blue:blue-team1"]["to"], "evidence/blue/blue-team1/")

    def test_recorded_but_vanished_run_dir_is_unrecoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            path, _ = ao.ensure_test(comp, kind="scrim")
            ao.record_paths(path, run_dir="/nonexistent/run")
            by_name = {t["name"]: t for t in ao.plan_targets(ao.load_manifest(path),
                                                             comp_dir=comp)}
            self.assertEqual(by_name["harness"]["status"], ao.UNRECOVERABLE)


class TestCollect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.comp = make_comp(self.tmp.name)
        self.path, _ = ao.ensure_test(self.comp, kind="scrim")
        ao.update_manifest(self.path, agents={
            "red": {"present": True, "vmid": 999, "node": "pve", "ssh": {"host": "10.0.0.198"}},
            "blue": {"present": False}})
        self.targets = [t for t in ao.plan_targets(ao.load_manifest(self.path),
                                                   comp_dir=self.comp)
                        if t["name"] == "red01"]

    def test_newest_report_becomes_the_canonical_document(self):
        calls = []
        files = {"/var/lib/bad-auto/report-20261003-0100.md": "old",
                 "/var/lib/bad-auto/report-20261003-0200.md": "new",
                 "/var/lib/bad-auto/events.jsonl": '{"a":1}\n'}
        os.utime(self.path, None)
        collection = ao.collect(self.path, self.targets,
                                transport=fake_transport(files, calls))
        self.assertEqual((self.path / "RED-TEAM.md").read_text().strip(), "new")
        derived = collection["derived"][0]
        self.assertEqual(derived["path"], "RED-TEAM.md")
        self.assertEqual(derived["method"], "pulled")
        self.assertTrue(derived["sha256"])
        self.assertEqual(collection["targets"][0]["status"], ao.OK)
        self.assertIn(RED_REPORT, calls)

    def test_missing_source_file_is_absent_not_failed(self):
        collection = ao.collect(self.path, self.targets, transport=fake_transport({}))
        statuses = {i["want"]: i["status"] for i in collection["targets"][0]["files"]}
        self.assertEqual(statuses[RED_REPORT], ao.ABSENT)
        self.assertEqual(collection["targets"][0]["status"], ao.ABSENT,
                         "a reachable source with nothing to give is absent, not a failure")
        self.assertNotIn(ao.ABSENT, ao.LOST)

    def test_unreachable_source_is_recorded_as_such(self):
        collection = ao.collect(self.path, self.targets,
                                transport=fake_transport({}, unreachable=True))
        self.assertEqual(collection["targets"][0]["status"], ao.UNREACHABLE)
        self.assertFalse((self.path / "RED-TEAM.md").exists(),
                         "collect() must not fabricate a canonical document")

    def test_second_run_does_not_refetch_and_keeps_provenance(self):
        calls = []
        files = {"/var/lib/bad-auto/report-20261003-0200.md": "new"}
        first = ao.collect(self.path, self.targets, transport=fake_transport(files, calls))
        sha = first["derived"][0]["sha256"]
        calls.clear()
        second = ao.collect(self.path, self.targets, transport=fake_transport(files, calls))
        self.assertNotIn(RED_REPORT, calls, "a re-run must not re-fetch what it already has")
        self.assertEqual(second["summary"]["reverified"], 1)
        self.assertEqual(second["derived"][0]["sha256"], sha,
                         "the hash record must survive a re-run")
        self.assertTrue(second["derived"][0]["reverified_at"])

    def test_tampered_artifact_is_recollected_rather_than_trusted(self):
        files = {"/var/lib/bad-auto/report-20261003-0200.md": "new"}
        ao.collect(self.path, self.targets, transport=fake_transport(files))
        collected = self.path / "evidence" / "red" / "report-20261003-0200.md"
        collected.write_text("tampered")
        calls = []
        second = ao.collect(self.path, self.targets, transport=fake_transport(files, calls))
        self.assertIn(RED_REPORT, calls, "hash drift must force a re-fetch")
        self.assertEqual(collected.read_text(), "new")
        self.assertTrue(second["derived"][0]["sha256"])

    def test_dry_run_touches_nothing(self):
        files = {"/var/lib/bad-auto/report-20261003-0200.md": "new"}
        collection = ao.collect(self.path, self.targets, transport=fake_transport(files),
                                dry_run=True)
        self.assertTrue(all(i["status"] == ao.SKIPPED
                            for i in collection["targets"][0]["files"]))
        self.assertEqual(list((self.path / "evidence" / "red").glob("report-*")), [])
        self.assertFalse((self.path / "RED-TEAM.md").exists())

    def test_local_files_are_sealed_0600(self):
        workdir = Path(self.tmp.name) / "blue-team1"
        workdir.mkdir()
        (workdir / "LOG.md").write_text("blue log")
        targets = [{"name": "blue:blue-team1", "route": "local", "kind": "local",
                    "root": str(workdir), "to": "evidence/blue/blue-team1/",
                    "want": [{"local_name": "LOG.md"}]}]
        ao.collect(self.path, targets)
        sealed = self.path / "evidence" / "blue" / "blue-team1" / "LOG.md"
        self.assertEqual(sealed.read_text(), "blue log")
        self.assertEqual(sealed.stat().st_mode & 0o777, 0o600)

    def test_guest_route_refuses_a_glob_instead_of_silently_returning_nothing(self):
        targets = [{"name": "web01", "route": "guest-agent", "kind": "remote", "node": "pve",
                    "vmid": 1220, "want": [{"remote": "/tmp/report-*.md",
                                            "local": "evidence/blue/report.md"}]}]
        collection = ao.collect(self.path, targets)
        item = collection["targets"][0]["files"][0]
        self.assertEqual(item["status"], ao.FAILED)
        self.assertIn("cannot expand", item["reason"])

    def test_unknown_route_is_a_hard_error(self):
        with self.assertRaises(RuntimeError):
            ao.collect(self.path, [{"name": "x", "route": "carrier-pigeon", "want": [
                {"remote": "/x", "local": "evidence/x"}]}])

    def test_red_journal_is_captured_through_the_command_channel(self):
        files = {"/var/lib/bad-auto/events.jsonl": '{"a":1}\n'}
        calls = []
        collection = ao.collect(self.path, self.targets,
                                transport=fake_transport(files, calls,
                                                         journal="unit bad-auto lines\n"))
        journal = self.path / "evidence" / "red" / "bad-auto-journal.log"
        self.assertEqual(journal.read_text(), "unit bad-auto lines\n")
        self.assertIn("journalctl -u bad-auto", " ".join(calls))
        statuses = {i["want"]: i["status"] for i in collection["targets"][0]["files"]}
        self.assertEqual(statuses["sudo -n journalctl -u bad-auto --no-pager"], ao.OK)

    def test_unreachable_target_is_not_probed_for_every_remaining_file(self):
        """A dead red01 must not cost four more connection timeouts inside a teardown."""
        collection = ao.collect(self.path, self.targets,
                                transport=fake_transport({}, unreachable=True))
        statuses = [i["status"] for i in collection["targets"][0]["files"]]
        self.assertEqual(statuses.count(ao.UNREACHABLE), 1)
        self.assertTrue(all(s == ao.SKIPPED for s in statuses[1:]), statuses)

    def test_ssh_capture_writes_stdout_and_targets_the_recorded_host(self):
        class Result:
            returncode = 0
            stdout = "journal line\n"
            stderr = ""

        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return Result()

        dest = Path(self.tmp.name) / "out" / "bad-auto-journal.log"
        with patch.object(ao.subprocess, "run", fake_run):
            written = ao._ssh_capture({"host": "10.0.0.198", "user": "sysadmin",
                                       "key": "/tmp/proxmox"}, "journalctl -u bad-auto", dest)
        self.assertEqual(written, [dest])
        self.assertEqual(dest.read_text(), "journal line\n")
        self.assertIn("sysadmin@10.0.0.198", seen["cmd"])
        self.assertIn("journalctl -u bad-auto", seen["cmd"])

    def test_ssh_capture_empty_output_is_absent_not_an_empty_file(self):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        dest = Path(self.tmp.name) / "out" / "empty.log"
        with patch.object(ao.subprocess, "run", lambda cmd, **kw: Result()):
            with self.assertRaises(FileNotFoundError):
                ao._ssh_capture({"host": "10.0.0.198"}, "journalctl", dest)
        self.assertFalse(dest.exists())


class TestWarningsAndStubs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.comp = make_comp(self.tmp.name)
        self.path, _ = ao.ensure_test(self.comp, kind="scrim")

    def test_missing_report_for_a_present_agent_is_a_loud_warning(self):
        ao.update_manifest(self.path, agents={"red": {"present": True,
                                                      "ssh": {"host": "10.0.0.198"}},
                                             "blue": {"present": False}})
        from unittest.mock import patch as _patch
        with _patch.object(ao, "plan_targets", ao.plan_targets):
            collection = ao.collect(self.path, ao.plan_targets(ao.load_manifest(self.path),
                                                              comp_dir=self.comp),
                                    transport=fake_transport({}, unreachable=True))
        warnings = ao.warn_summary(collection, ao.load_manifest(self.path))
        self.assertTrue(any("RED-TEAM.md is ABSENT although this run had a red agent" in w
                            for w in warnings), warnings)
        # And with the stub in place the warning carries the reason the pull failed.
        ao.finalize(self.path, manifest=ao.load_manifest(self.path))
        warnings = ao.warn_summary(collection, ao.load_manifest(self.path), self.path)
        self.assertTrue(any("RED-TEAM.md is UNREACHABLE" in w for w in warnings), warnings)

    def test_no_agent_means_no_warning(self):
        ao.update_manifest(self.path, agents={"red": {"present": False},
                                              "blue": {"present": False}})
        collection = ao.collect(self.path, ao.plan_targets(ao.load_manifest(self.path),
                                                          comp_dir=self.comp))
        self.assertEqual(ao.warn_summary(collection, ao.load_manifest(self.path)), [])

    def test_stub_says_why_a_document_is_absent(self):
        ao.update_manifest(self.path, agents={"red": {"present": False},
                                              "blue": {"present": False}})
        ao.collect(self.path, ao.plan_targets(ao.load_manifest(self.path), comp_dir=self.comp))
        ao.finalize(self.path, manifest=ao.load_manifest(self.path))
        text = (self.path / "RED-TEAM.md").read_text()
        self.assertIn("status: skipped", text)
        self.assertIn("no red agent", text)

    def test_an_uncollected_report_is_not_called_absent(self):
        """The source is still out there: saying "absent" would be a claim about a box nobody
        asked. The stub must name the command that fixes it."""
        ao.update_manifest(self.path, agents={"red": {"present": False},
                                              "blue": {"present": True, "teams": 1}})
        workdir = Path(self.tmp.name) / "blue-team1"
        workdir.mkdir()
        (workdir / "REPORT.md").write_text("# blue after-action\n")
        ao.record_paths(self.path, blue_workdirs=[str(workdir)])
        ao.finalize(self.path, manifest=ao.load_manifest(self.path))
        text = (self.path / "BLUE-TEAM.md").read_text()
        self.assertIn("status: not-collected", text)
        self.assertIn("test-artifacts.py collect", text)

    def test_a_stub_is_not_reported_as_a_present_report(self):
        ao.update_manifest(self.path, agents={"red": {"present": True,
                                                      "ssh": {"host": "10.0.0.198"}},
                                              "blue": {"present": False}})
        collection = ao.collect(self.path, ao.plan_targets(ao.load_manifest(self.path),
                                                           comp_dir=self.comp),
                                transport=fake_transport({}, unreachable=True))
        ao.finalize(self.path, manifest=ao.load_manifest(self.path))
        warnings = ao.warn_summary(collection, ao.load_manifest(self.path), self.path)
        self.assertTrue(any("RED-TEAM.md is UNREACHABLE" in w for w in warnings), warnings)

    def test_a_wholesale_target_failure_is_not_reported_as_absent(self):
        """red01 with no recorded address is never probed, so no per-file record exists — and
        calling that "absent" would claim a box that was never asked. The target's own status is
        the honest answer for the document it was meant to carry (found by the CLI agent)."""
        ao.update_manifest(self.path, agents={"red": {"present": True, "ssh": {}},
                                              "blue": {"present": False}})
        targets = [{"name": "red01", "route": "scp-jump", "kind": "remote", "ssh": {},
                    "want": [{"remote": RED_REPORT, "to": "evidence/red/",
                              "canonical": "RED-TEAM.md"}]}]
        ao.collect(self.path, targets, transport=fake_transport({}))
        ao.finalize(self.path, manifest=ao.load_manifest(self.path))
        text = (self.path / "RED-TEAM.md").read_text()
        self.assertIn("status: unreachable", text)
        self.assertNotIn("status: absent", text)

    def test_a_collected_report_is_not_warned_about(self):
        ao.update_manifest(self.path, agents={"red": {"present": True,
                                                      "ssh": {"host": "10.0.0.198"}},
                                              "blue": {"present": False}})
        targets = ao.plan_targets(ao.load_manifest(self.path), comp_dir=self.comp)
        files = {"/var/lib/bad-auto/report-20261003-0200.md": "the red report"}
        collection = ao.collect(self.path, targets, transport=fake_transport(files))
        ao.finalize(self.path, manifest=ao.load_manifest(self.path))
        self.assertEqual(ao.warn_summary(collection, ao.load_manifest(self.path), self.path), [])
        text = (self.path / "RED-TEAM.md").read_text()
        self.assertEqual(text, "the red report")

    def test_unrecoverable_source_produces_an_honest_stub(self):
        ao.update_manifest(self.path, agents={"red": {"present": True,
                                                      "ssh": {"host": "10.0.0.198"}},
                                             "blue": {"present": False}})
        aos = self.path / "evidence"
        targets = [{"name": "red01", "route": "scp-jump", "kind": "remote",
                    "ssh": {"host": "10.0.0.198"}, "want": [{"remote": RED_REPORT,
                                                             "to": "evidence/red/",
                                                             "canonical": "RED-TEAM.md"}]}]
        with patch.object(ao, "plan_targets", lambda manifest, comp_dir=None: targets):
            ao.collect(self.path, targets, transport=fake_transport({}, unreachable=True))
            ao.finalize(self.path, manifest=ao.load_manifest(self.path))
        text = (self.path / "RED-TEAM.md").read_text()
        self.assertIn("status: unreachable", text)
        self.assertTrue(aos.is_dir())


class TestReportAndSeal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.comp = make_comp(self.tmp.name)
        self.path, _ = ao.ensure_test(self.comp, kind="scrim")
        ao.update_manifest(self.path, agents={"red": {"present": False},
                                              "blue": {"present": False}})
        ao.collect(self.path, ao.plan_targets(ao.load_manifest(self.path), comp_dir=self.comp))
        ao.finalize(self.path, manifest=ao.load_manifest(self.path))

    def test_skeleton_carries_machine_facts_and_marks_judgement(self):
        text = (self.path / "REPORT.md").read_text()
        self.assertIn("## Recommendations — tezcatlipoca", text)
        self.assertIn("## Recommendations — this competition", text)
        self.assertIn("## Incidents", text)
        self.assertIn("TODO(author)", text)
        self.assertIn("No red agent took part in this run.", text)

    def test_verdict_is_lifted_verbatim_from_interaction_md(self):
        harness = self.path / "evidence" / "harness"
        (harness / "INTERACTION.md").write_text(
            "# INTERACTION\n\n## Verdict\n\n**GREEN** — score 11\n\n## Red\n\nstuff\n")
        ao.write_report_skeleton(self.path, force=True)
        self.assertIn("**GREEN** — score 11", (self.path / "REPORT.md").read_text())

    def test_an_authored_report_is_never_clobbered(self):
        report = self.path / "REPORT.md"
        report.write_text("# my careful write-up\n")
        ao.write_report_skeleton(self.path)
        self.assertEqual(report.read_text(), "# my careful write-up\n")
        ao.seal_test(self.path, author="tester")
        ao.write_report_skeleton(self.path)
        self.assertEqual(report.read_text(), "# my careful write-up\n")

    def test_seal_refuses_while_judgement_is_unfilled_then_force_works(self):
        with self.assertRaises(RuntimeError) as ctx:
            ao.seal_test(self.path, author="tester")
        self.assertIn("TODO(author)", str(ctx.exception))
        result = ao.seal_test(self.path, author="tester", force=True)
        self.assertEqual(result["author"], "tester")
        manifest = ao.load_manifest(self.path)
        self.assertEqual(manifest["writeup"]["status"], "done")

    def test_verify_flags_tampering_and_reports_the_seal_state(self):
        run = Path(self.tmp.name) / "run"
        run.mkdir()
        (run / "run.json").write_text('{"ok": true}')
        ao.record_paths(self.path, run_dir=str(run))
        ao.collect(self.path, ao.plan_targets(ao.load_manifest(self.path), comp_dir=self.comp))
        collected = self.path / "evidence" / "harness" / "run.json"
        self.assertEqual(collected.read_text(), '{"ok": true}')
        collected.write_text('{"ok": false}')
        problems = ao.verify_test(self.path)["problems"]
        self.assertTrue(any("hash drift" in p for p in problems), problems)


class TestIndexAndArchive(unittest.TestCase):
    def test_index_lists_newest_first_and_counts_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            older = ao.ensure_test(comp, kind="deploy", key="untagged-20260101-000000")[0]
            newer = ao.ensure_test(comp, kind="scrim", key="untagged-20260202-000000")[0]
            ao.save_manifest(older, {**ao.load_manifest(older),
                                     "created_at": "2026-01-01T00:00:00"})
            ao.save_manifest(newer, {**ao.load_manifest(newer),
                                     "created_at": "2026-02-02T00:00:00"})
            rows = ao.list_tests(comp)
            self.assertEqual([r["key"] for r in rows],
                             ["untagged-20260202-000000", "untagged-20260101-000000"])
            self.assertTrue(all(r["writeup"] == "needs-writeup" for r in rows))

    def test_archive_copies_and_verifies_every_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            path, _ = ao.ensure_test(comp, kind="scrim")
            (path / "evidence" / "red" / "events.jsonl").write_text('{"a":1}\n')
            (path / "REPORT.md").write_text("# report\n")
            dest = ao.archive_test(comp, "run-1a2b3c4d", dest_root=Path(tmp) / "archive")
            self.assertTrue((dest / "REPORT.md").exists())
            self.assertEqual((dest / "evidence" / "red" / "events.jsonl").read_text(),
                             '{"a":1}\n')
            self.assertTrue(str(dest).startswith(str(Path(tmp) / "archive")))

    def test_archive_verification_failure_is_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            ao.ensure_test(comp, kind="scrim")
            with patch.object(ao.shutil, "copytree", lambda src, dst: None):
                with self.assertRaises(RuntimeError) as ctx:
                    ao.archive_test(comp, "run-1a2b3c4d", dest_root=Path(tmp) / "archive")
            self.assertIn("archive verification failed", str(ctx.exception))


class TestWorktreeDetection(unittest.TestCase):
    def _run(self, mapping):
        class Result:
            def __init__(self, stdout):
                self.stdout = stdout
                self.returncode = 0

        def fake_run(cmd, **kwargs):
            for needle, out in mapping.items():
                if needle in cmd:
                    return Result(out)
            return Result("")

        return fake_run

    def test_linked_worktree_is_detected(self):
        mapping = {"--git-dir": "/repo/.git/worktrees/at", "--git-common-dir": "/repo/.git"}
        with patch.object(ao.subprocess, "run", self._run(mapping)):
            self.assertTrue(ao.in_worktree())

    def test_main_checkout_is_not_a_worktree(self):
        mapping = {"--git-dir": "/repo/.git", "--git-common-dir": "/repo/.git"}
        with patch.object(ao.subprocess, "run", self._run(mapping)):
            self.assertFalse(ao.in_worktree())


class TestVerdictIngestion(unittest.TestCase):
    INTERACTION = """# INTERACTION — run-1a2b3c4d

Generated now from 12 red events.

## Verdict

**NOT READY — interaction score 7, 2 gate(s) failed** (see Gates below).

Interaction score components: restorations 2, evictions 1.

## Gates (docs/rehearsal-gates.md)

| side | gate | value | threshold | verdict |
|---|---|---|---|---|
| red | takedowns | 6 | >= 6 | PASS |
| red | evictions | 0 | >= 1 | FAIL |
| blue | cycles rc=0 | 3 | >= 8 | FAIL |
| blue | notebook entries | n/a | >= 10 | n/a |

## Not verified

nothing
"""

    def test_parses_status_score_and_gate_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            path, _ = ao.ensure_test(comp, kind="scrim")
            harness = path / "evidence" / "harness"
            harness.mkdir(parents=True, exist_ok=True)
            (harness / "INTERACTION.md").write_text(self.INTERACTION)
            verdict = ao.ingest_verdict(path)
            self.assertEqual(verdict["status"], "NOT READY")
            self.assertEqual(verdict["score"], 7)
            self.assertEqual((verdict["gates_passed"], verdict["gates_failed"],
                              verdict["gates_na"]), (1, 2, 1))
            self.assertEqual(ao.load_manifest(path)["verdict"]["status"], "NOT READY")

    def test_a_run_with_no_report_simply_has_no_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            path, _ = ao.ensure_test(comp, kind="deploy")
            self.assertEqual(ao.ingest_verdict(path), {})
            self.assertEqual(ao.load_manifest(path)["verdict"], {})

    def test_verdict_lands_in_the_report_skeleton(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp)
            path, _ = ao.ensure_test(comp, kind="scrim")
            harness = path / "evidence" / "harness"
            harness.mkdir(parents=True, exist_ok=True)
            (harness / "INTERACTION.md").write_text(self.INTERACTION)
            ao.ingest_verdict(path)
            ao.update_manifest(path, agents={"red": {"present": False},
                                             "blue": {"present": False}})
            ao.finalize(path, manifest=ao.load_manifest(path))
            text = (path / "REPORT.md").read_text()
            self.assertIn("**NOT READY** — interaction score 7; gates 1 pass / 2 fail / 1 n/a",
                          text)


class TestTeardownEntryPoint(unittest.TestCase):
    """collect_for_teardown is the call destroy-competition.py makes; it must leave a complete
    folder even when the harness that normally fills it never ran."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.comp = make_comp(self.tmp.name)
        self.run = Path(self.tmp.name) / "run"
        (self.run / "evidence").mkdir(parents=True)
        (self.run / "run.json").write_text(json.dumps({"competition": "demo"}))
        (self.run / "evidence" / "alerts.jsonl").write_text(
            json.dumps({"kind": "red_llm_restart"}) + "\n")
        blue = self.run / "blue-team1"
        blue.mkdir()
        (blue / "REPORT.md").write_text("# blue after-action\n")
        path, _ = ao.ensure_test(self.comp, kind="scrim", script="run-agent-scrim.py", teams=1,
                                 boxes=["dc01"], node="pve")
        self.path = path
        ao.update_manifest(path, agents={"red": {"present": False},
                                         "blue": {"present": True, "teams": 1}})
        ao.record_paths(path, run_dir=str(self.run), blue_workdirs=[str(blue)])

    def test_it_produces_a_complete_folder_for_a_plain_run(self):
        ao.collect_for_teardown(self.comp, run_id="run-1a2b3c4d",
                                teams={"team1": {"identifier": "101"}},
                                boxes=[{"name": "dc01"}], node="pve", echo=lambda *a: None)
        self.assertTrue((self.path / "REPORT.md").exists())
        self.assertEqual((self.path / "BLUE-TEAM.md").read_text(), "# blue after-action\n")
        self.assertTrue((self.path / "evidence" / "blue" / "blue-team1" / "REPORT.md").exists())
        self.assertEqual(ao.load_manifest(self.path)["teardown"]["by"],
                         "destroy-competition.py")

    def test_it_runs_scrim_report_when_the_harness_did_not(self):
        """A crashed harness leaves no INTERACTION.md; the report generator only needs the run
        dir, so teardown runs it rather than shipping a verdict-less report."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)

            class Result:
                returncode = 0
                stdout = "wrote INTERACTION.md\n"
                stderr = ""

            (self.run / "INTERACTION.md").write_text(
                "# INTERACTION\n\n## Verdict\n\n**GREEN — interaction score 11, all gates "
                "passed.**\n\n## Gates\n\n| side | gate | value | threshold | verdict |\n"
                "|---|---|---|---|---|\n| red | takedowns | 9 | >= 6 | PASS |\n")
            return Result()

        with patch.object(ao.subprocess, "run", fake_run):
            ao.collect_for_teardown(self.comp, run_id="run-1a2b3c4d", echo=lambda *a: None)
        self.assertIn("scrim-report.py", " ".join(calls[0]))
        self.assertEqual(ao.load_manifest(self.path)["verdict"]["status"], "GREEN")
        self.assertIn("**GREEN**", (self.path / "REPORT.md").read_text())

    def test_a_report_generator_failure_only_warns(self):
        def boom(cmd, **kwargs):
            raise OSError("python3 disappeared")

        lines = []
        with patch.object(ao.subprocess, "run", boom):
            ao.collect_for_teardown(self.comp, run_id="run-1a2b3c4d", echo=lines.append)
        self.assertTrue(any("no machine verdict" in line for line in lines), lines)
        self.assertTrue((self.path / "REPORT.md").exists())

    def test_dry_run_contacts_nothing_and_writes_no_report_generator_output(self):
        calls = []

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return Result()

        with patch.object(ao.subprocess, "run", fake_run):
            collection = ao.collect_for_teardown(self.comp, run_id="run-1a2b3c4d",
                                                 dry_run=True, echo=lambda *a: None)
        self.assertEqual([c for c in calls if "scrim-report.py" in " ".join(c)], [])
        self.assertTrue(all(i["status"] == ao.SKIPPED
                            for t in collection["targets"] for i in t["files"]))


if __name__ == "__main__":
    unittest.main()


class ScpRetry(unittest.TestCase):
    """The route pair retries once on a network-side flap (2026-10-03: the red01
    pull died on a seconds-long no-route flap on the ENGINE's jump address and the
    red report was lost; the destroy that ran next reached the same engine fine).

    ao.time is the GLOBAL time module — patches must restore it or every later
    test in the process inherits a poisoned time.sleep (live-found in this same
    session: the full suite went 13-red until this was scoped)."""

    def _fake_run(self, successes_after, out_file):
        """subprocess.run stub: from call N on it succeeds by creating the file;
        earlier calls answer rc=1 with a no-route stderr."""
        calls = {"n": 0}

        def run(cmd, capture_output=True, text=True, timeout=None):
            calls["n"] += 1
            if calls["n"] > successes_after:
                Path(out_file).write_text("report body")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr="ssh: connect to host 10.0.0.250 port 22: "
                                          "No route to host")
        return run

    def test_flap_on_both_routes_recovers_on_the_retry_round(self):
        import artifacts_ops as ao
        d = Path(tempfile.mkdtemp())
        sleeps = []
        # succeeds on the retry round's last attempt (2 routes x 2 rounds)
        with patch.object(ao.subprocess, "run",
                               self._fake_run(3, str(d / "report.md"))), \
                patch.object(ao.time, "sleep", side_effect=sleeps.append):
            got = ao._scp_files({"key": "/k", "user": "sysadmin", "host": "10.0.0.198",
                                 "jump": "jump-opt"}, "/var/lib/bad-auto/report-*.md", d)
        self.assertEqual([p.name for p in got], ["report.md"])
        self.assertEqual(sleeps, [10], "exactly one inter-round pause")

    def test_still_unreachable_raises_with_the_last_error(self):
        import artifacts_ops as ao
        d = Path(tempfile.mkdtemp())

        def always_fail(cmd, capture_output=True, text=True, timeout=None):
            return SimpleNamespace(returncode=1, stdout="", stderr="No route to host")

        with patch.object(ao.subprocess, "run", always_fail), \
                patch.object(ao.time, "sleep", lambda s: None):
            with self.assertRaises(ao.Unreachable) as raised:
                ao._scp_files({"key": "/k", "user": "sysadmin", "host": "10.0.0.198"},
                              "/var/lib/bad-auto/report-*.md", d)
        self.assertIn("No route to host", str(raised.exception))
