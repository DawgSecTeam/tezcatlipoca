"""Per-run test artifacts (artifacts_lib/artifacts_ops.py): the collector that runs at teardown.

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

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from artifacts_lib import artifacts_ops as ao
from estate import state as estate_state

# the red artifacts are pulled from the sudo-staged copies (/tmp/ba): the originals
# are 0600 root, and one unreadable file must not mark the whole target unreachable
RED_REPORT = "/tmp/ba/report.md"


def make_comp(root, *, run_id="run-1a2b3c4d", comp="demo"):
    comp = Path(root) / "competitions" / comp
    comp.mkdir(parents=True, exist_ok=True)
    (comp / "Compfile").write_text("name demo-comp\nscenario x\n")
    state = {"teams": {"team1": {"identifier": "101"}}}
    if run_id:
        state["run_id"] = run_id
    estate_state.write_flat(comp, state)
    return comp


def fake_transport(files, calls=None, unreachable=False, journal=None):
# A transport whose `scp-jump` route answers from `files` (glob keys allowed).
#
#     `ssh-cmd` is faked too, and deliberately: a real one would dial red01 from a unit test. It
#     answers `journal` when given, else reports the command as having produced nothing.
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
    # A state document without a run id, and teardown is re-run until clean — a fresh
    #         timestamp per invocation would split one run's evidence.
        with tempfile.TemporaryDirectory() as tmp:
            comp = make_comp(tmp, run_id=None)
            first = ao.test_key(comp)
            time.sleep(0.01)
            self.assertEqual(first, ao.test_key(comp))
            self.assertTrue(first.startswith("untagged-"), first)




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
    # The manifest is read from a different CWD than the one that wrote it (teardown runs
    #         from the repo root, the harness from its worktree), so a relative path here is a bug
    #         waiting for a reader in the wrong directory.
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
            self.assertIn("journalctl -u tlaloc",
                          " ".join(w.get("cmd") or "" for w in red_want))
            self.assertEqual(by_name["red01"]["ssh"]["host"], "10.0.0.198")
            self.assertEqual(by_name["blue:blue-team1"]["to"], "evidence/blue/blue-team1/")



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

    def test_the_report_becomes_the_canonical_document(self):
        calls = []
        files = {"/tmp/ba/report.md": "new",
                 "/tmp/ba/events.jsonl": '{"a":1}\n'}
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


    def test_second_run_does_not_refetch_and_keeps_provenance(self):
        calls = []
        files = {"/tmp/ba/report.md": "new"}
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
        files = {"/tmp/ba/report.md": "new"}
        ao.collect(self.path, self.targets, transport=fake_transport(files))
        collected = self.path / "evidence" / "red" / "report.md"
        collected.write_text("tampered")
        calls = []
        second = ao.collect(self.path, self.targets, transport=fake_transport(files, calls))
        self.assertIn(RED_REPORT, calls, "hash drift must force a re-fetch")
        self.assertEqual(collected.read_text(), "new")
        self.assertTrue(second["derived"][0]["sha256"])

    def test_dry_run_touches_nothing(self):
        files = {"/tmp/ba/report.md": "new"}
        collection = ao.collect(self.path, self.targets, transport=fake_transport(files),
                                dry_run=True)
        self.assertTrue(all(i["status"] == ao.SKIPPED
                            for i in collection["targets"][0]["files"]))
        self.assertEqual(list((self.path / "evidence" / "red").glob("report.md")), [])
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

    def test_unknown_route_is_a_hard_error(self):
        with self.assertRaises(RuntimeError):
            ao.collect(self.path, [{"name": "x", "route": "carrier-pigeon", "want": [
                {"remote": "/x", "local": "evidence/x"}]}])


    def test_unreachable_target_is_not_probed_for_every_remaining_file(self):
    # A dead red01 must not cost four more connection timeouts inside a teardown.
        collection = ao.collect(self.path, self.targets,
                                transport=fake_transport({}, unreachable=True))
        statuses = [i["status"] for i in collection["targets"][0]["files"]]
        self.assertEqual(statuses.count(ao.UNREACHABLE), 1)
        self.assertTrue(all(s == ao.SKIPPED for s in statuses[1:]), statuses)


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


    def test_a_synthesized_blue_narrative_reaches_the_canonical_document(self):
        # Blue is present, every cycle timed out, so blue itself wrote no REPORT.md; the
        # harness's narrative must still land as BLUE-TEAM.md and clear the collector's
        # "present agent, missing canonical document" guard caught live.
        from scrim import blue_agent
        ao.update_manifest(self.path, agents={"red": {"present": False},
                                              "blue": {"present": True, "teams": 1}})
        workdir = Path(self.tmp.name) / "blue-team1"
        workdir.mkdir()
        (workdir / "feed.log").write_text("\n===== cycle T+0 TIMEOUT =====\n")
        (workdir / "LOG.md").write_text("# Blue team 1 — defense log\nnothing yet\n")
        ao.record_paths(self.path, blue_workdirs=[str(workdir)])
        blue_agent.finalize_blue_narratives(
            SimpleNamespace(run_dir=str(workdir.parent), teams=1, test_dir=str(self.path)))
        manifest = ao.load_manifest(self.path)
        collection = ao.collect(self.path, ao.plan_targets(manifest, comp_dir=self.comp))
        ao.finalize(self.path, manifest=manifest)
        body = (self.path / "BLUE-TEAM.md").read_text()
        self.assertIn("harness-synthesized", body)
        self.assertIn("TIMEOUT", body)
        warnings = ao.warn_summary(collection, manifest, self.path)
        self.assertFalse(any("BLUE-TEAM.md" in w for w in warnings), warnings)



    def test_an_uncollected_report_is_not_called_absent(self):
    # The source is still out there: saying "absent" would be a claim about a box nobody
    #         asked. The stub must name the command that fixes it.
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

    def test_a_wholesale_target_failure_is_not_reported_as_absent(self):
    # red01 with no recorded address is never probed, so no per-file record exists — and
    #         calling that "absent" would claim a box that was never asked. The target's own status is
    #         the honest answer for the document it was meant to carry (found by the CLI agent).
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

class TestIndexAndArchive(unittest.TestCase):

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


class TestVerdictIngestion(unittest.TestCase):
    INTERACTION = """# INTERACTION — the run that found it

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




class TestTeardownEntryPoint(unittest.TestCase):
# collect_for_teardown is the call destroy-competition.py makes; it must leave a complete
#     folder even when the harness that normally fills it never ran.

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

    def test_a_failed_transfer_is_retried_once(self):
        # `failed` means the source answered and the transfer broke: transient, and the destroy
        # that follows would make a second attempt impossible.
        real = ao.default_transport()
        attempts = []

        def flaky_local(source, pattern, dest_dir):
            if pattern == "run.json":
                attempts.append(pattern)
                if len(attempts) == 1:
                    raise OSError("disk hiccup")
            return real["local"](source, pattern, dest_dir)

        with patch.object(ao, "default_transport", lambda: dict(real, local=flaky_local)):
            collection = ao.collect_for_teardown(self.comp, run_id="run-1a2b3c4d",
                                                 echo=lambda *a: None)
        self.assertEqual(len(attempts), 2)
        self.assertEqual([i["status"] for t in collection["targets"] for i in t["files"]
                          if i["want"] == "run.json"], [ao.OK])



if __name__ == "__main__":
    unittest.main()


class ScpRetry(unittest.TestCase):
# The route pair retries once on a network-side flap (the red01
#     pull died on a seconds-long no-route flap on the ENGINE's jump address and the
#     red report was lost; the destroy that ran next reached the same engine fine).
#
#     ao.time is the GLOBAL time module — patches must restore it or every later
#     test in the process inherits a poisoned time.sleep (live-found in this same
#     session: the full suite went 13-red until this was scoped).

    def _fake_run(self, successes_after, out_file):
    # subprocess.run stub: from call N on it succeeds by creating the file;
    #         earlier calls answer rc=1 with a no-route stderr.
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
        from artifacts_lib import transport as ao
        d = Path(tempfile.mkdtemp())
        sleeps = []
        # succeeds on the retry round's last attempt (2 routes x 2 rounds)
        with patch.object(ao.subprocess, "run",
                               self._fake_run(3, str(d / "report.md"))), \
                patch.object(ao.time, "sleep", side_effect=sleeps.append):
            got = ao._scp_files({"key": "/k", "user": "sysadmin", "host": "10.0.0.198",
                                 "jump": "jump-opt"}, "/tmp/ba/report-*.md", d)
        self.assertEqual([p.name for p in got], ["report.md"])
        self.assertEqual(got[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(sleeps, [10], "exactly one inter-round pause")

