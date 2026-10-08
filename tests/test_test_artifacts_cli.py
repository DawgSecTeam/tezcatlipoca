"""test-artifacts.py CLI: the read/inspect/repair surface over `.automated-tests/<key>/`.

Fully offline: no estate, no Proxmox, no network. The CLI is called in-process (its exit codes are
*returned*, not raised) with the cwd moved into a temp repo-like root — which is how an operator
invokes it from the repo root, and the only way to exercise `competitions/<id>` resolution without
touching the real tree. `collect` is exercised live only for a comp whose manifest has no agents
(so no target has a remote route) and otherwise with `--dry-run`, because the real transport would
scp to a red01 VM.
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import artifacts_ops as ao

RUN_ID = "run-1a2b3c4d"
BUILD_INFO = '{"ok": true}\n'


def _load_cli():
    """The script is not importable by name (`test-artifacts.py` has a dash), so load it by path.

    Importing it is safe: only `main()` is guarded, and importing reads no state — artifacts_ops
    resolves its own paths from `__file__`, never from the cwd the tests move."""
    spec = importlib.util.spec_from_file_location("test_artifacts_cli_under_test",
                                                  _REPO / "test-artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


class CliTestCase(unittest.TestCase):
    """A temp `repo root` holding competitions/demo with a Compfile + a run id in deploy state."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.comp = self.root / "competitions" / "demo"
        self.comp.mkdir(parents=True)
        (self.comp / "Compfile").write_text("name demo-2026-10-03\nscenario demo\n")
        (self.comp / ".deploy_state.json").write_text(
            json.dumps({"run_id": RUN_ID, "teams": {"team1": {"identifier": "101"}}}))
        self._cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._cwd)
        # seal_test refreshes the durable archive when the checkout is a linked worktree; point
        # that root at the temp dir so the suite stays hermetic wherever it runs.
        patcher = patch.dict(os.environ,
                             {"TEZ_ARTIFACTS_ARCHIVE": str(self.root / "archive")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key = ao.test_key(self.comp)
        self.assertEqual(self.key, RUN_ID)

    # ── helpers ────────────────────────────────────────────────────────────────────────

    def run_cli(self, *argv):
        """(exit code, stdout+stderr) — both streams, so a refusal printed to stderr is visible."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = cli.main(list(argv))
        return code, buffer.getvalue()

    def make_folder(self, kind="deploy", *, files=None):
        """An `ensure_test` folder, optionally with a collectable local harness run dir."""
        path, _ = ao.ensure_test(self.comp, kind=kind, script="tests/test_test_artifacts_cli.py")
        if files is not None:
            run_dir = self.root / "run"
            run_dir.mkdir(exist_ok=True)
            for name, body in files.items():
                (run_dir / name).write_text(body)
            ao.record_paths(path, run_dir=str(run_dir))
        return path

    def settle(self, path):
        """Collect (local-only) then stubs/REPORT.md/index — what teardown leaves behind."""
        ao.collect(path, ao.plan_targets(ao.load_manifest(path), comp_dir=self.comp))
        ao.finalize(path, manifest=ao.load_manifest(path))
        return path

    def child(self, *parts):
        return self.root.joinpath(*parts)


class TestList(CliTestCase):
    def test_index_rows_for_one_comp(self):
        self.settle(self.make_folder(files={"run.json": BUILD_INFO, "monitor.log": "up\n"}))
        code, out = self.run_cli("list", "demo")
        self.assertEqual(code, 0, out)
        self.assertIn(RUN_ID, out)
        self.assertIn("deploy", out)
        self.assertIn("needs-writeup", out)
        self.assertTrue((self.comp / ".automated-tests" / "index.json").exists(),
                        "list must leave a rebuilt index behind")

    def test_json_rows_are_machine_readable(self):
        self.settle(self.make_folder())
        code, out = self.run_cli("list", "demo", "--json")
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertEqual([row["key"] for row in payload["tests"]], [RUN_ID])
        self.assertEqual(payload["tests"][0]["kind"], "deploy")

    def test_scan_without_comp_groups_by_competition(self):
        self.settle(self.make_folder(files={"run.json": BUILD_INFO}))
        other = self.comp.parent / "noartifacts"
        other.mkdir()
        (other / "Compfile").write_text("name other\nscenario x\n")
        code, out = self.run_cli("list")
        self.assertEqual(code, 0, out)
        self.assertIn("demo", out)
        self.assertIn(RUN_ID, out)
        self.assertNotIn("noartifacts", out,
                         "a comp with no .automated-tests/ folder is not part of the scan")

    def test_scan_with_no_artifacts_anywhere_says_so(self):
        code, out = self.run_cli("list")
        self.assertEqual(code, 0, out)
        self.assertIn("No competition under", out)


class TestShow(CliTestCase):
    def test_show_exits_zero_and_names_a_target_status(self):
        path = self.settle(self.make_folder(files={"run.json": BUILD_INFO, "monitor.log": "up\n"}))
        code, out = self.run_cli("show", "demo", RUN_ID)
        self.assertEqual(code, 0, out)
        self.assertIn("harness — local — ok", out)
        self.assertRegex(out, r"run\.json -> ok", "each file record must name what was wanted")
        self.assertIn("no problems", out)
        self.assertIn("Canonical documents", out)
        self.assertIn("REPORT.md", out)
        self.assertTrue((path / "REPORT.md").exists())

    def test_show_explains_a_missing_canonical_document(self):
        """The 'why is RED-TEAM.md missing?' case: a red agent that was never reachable."""
        path = self.make_folder()
        ao.update_manifest(path, agents={"red": {"present": True}, "blue": {"present": False}})
        # No ssh host recorded, so collect() marks red01 unreachable without touching a network.
        self.settle(path)
        code, out = self.run_cli("show", "demo", RUN_ID)
        self.assertEqual(code, 0, out)
        self.assertIn("red01 — scp-jump — unreachable", out)
        # The module owns the warning wording (MISSING vs ABSENT tracks the status vocabulary);
        # what this CLI owes the operator is that the line is passed through, not paraphrased.
        self.assertRegex(out, r"RED-TEAM\.md is (MISSING|ABSENT|UNREACHABLE)")
        stub = (path / "RED-TEAM.md").read_text()
        self.assertIn("side: red", stub)
        self.assertIn("not available", stub)
        # show must say the document on disk is a stub and which status that stub records.
        self.assertRegex(out, r"locally written stub: \S+")

    def test_show_json_carries_problems_and_todos(self):
        self.settle(self.make_folder(files={"run.json": BUILD_INFO}))
        code, out = self.run_cli("show", "demo", RUN_ID, "--json")
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertEqual(payload["problems"], [])
        self.assertGreater(payload["todos"], 0, "the skeleton must still hold TODO(author) marks")
        self.assertEqual(payload["identity"]["run_id"], RUN_ID)


class TestVerifyAndSeal(CliTestCase):
    def test_verify_fails_while_todos_remain_then_passes_when_filled(self):
        path = self.settle(self.make_folder(files={"run.json": BUILD_INFO}))
        code, out = self.run_cli("verify", "demo", RUN_ID)
        self.assertEqual(code, 1, out)
        self.assertIn("TODO(author)", out)

        (path / "REPORT.md").write_text(
            "# run report\n\n## Verdict\n\n**GREEN** — measured.\n\n## Recommendations\n\nnone\n")
        code, out = self.run_cli("verify", "demo", RUN_ID)
        self.assertEqual(code, 0, out)
        self.assertIn("PASS", out)

    def test_verify_reports_hash_drift(self):
        path = self.settle(self.make_folder(files={"run.json": BUILD_INFO}))
        (path / "evidence" / "harness" / "run.json").write_text('{"ok": false}\n')
        code, out = self.run_cli("verify", "demo", RUN_ID)
        self.assertEqual(code, 1, out)
        self.assertIn("hash drift", out)

    def test_seal_refuses_unfilled_todos_then_force_seals(self):
        path = self.settle(self.make_folder())
        code, out = self.run_cli("verify", "demo", RUN_ID, "--seal", "--author", "tester")
        self.assertEqual(code, 1, out)
        self.assertIn("SEAL REFUSED", out)
        self.assertIn("TODO(author)", out)
        self.assertEqual(ao.load_manifest(path)["writeup"]["status"], "needs-writeup",
                         "a refused seal must not touch test.json")

        code, out = self.run_cli("verify", "demo", RUN_ID, "--seal", "--author", "tester",
                                 "--force")
        self.assertEqual(code, 0, out)
        self.assertIn("SEALED", out)
        self.assertIn("waived", out)
        writeup = ao.load_manifest(path)["writeup"]
        self.assertEqual(writeup["status"], "done")
        self.assertEqual(writeup["author"], "tester")


class TestPlan(CliTestCase):
    def test_plan_prints_the_skipped_reason_for_a_run_with_no_agents(self):
        self.make_folder(kind="deploy")
        code, out = self.run_cli("plan", "demo", RUN_ID)
        self.assertEqual(code, 0, out)
        self.assertIn("skipped", out)
        self.assertIn("no red agent in this run", out)
        self.assertIn("would attempt NOW", out)
        self.assertIn("worktree", out, "the archive hint must reach the operator either way")

    def test_plan_lists_wanted_items_for_a_present_agent(self):
        path = self.make_folder()
        ao.update_manifest(path, agents={"red": {"present": True,
                                                 "ssh": {"host": "10.0.0.198"}},
                                         "blue": {"present": False}})
        code, out = self.run_cli("plan", "demo", RUN_ID)
        self.assertEqual(code, 0, out)
        self.assertIn("/tmp/ba/report-*.md", out)   # the sudo-staged, readable copies
        self.assertIn("evidence/red/", out)
        self.assertNotRegex(out, r"(?m)^\s+\? ->",
                            "every wanted item must be named, whatever shape its want dict has")


class TestCollect(CliTestCase):
    def test_dry_run_records_skipped_and_fetches_nothing(self):
        path = self.make_folder(files={"run.json": BUILD_INFO})
        code, out = self.run_cli("collect", "demo", RUN_ID, "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("DRY RUN", out)
        collection = ao.load_collection(path)
        statuses = {item["status"] for target in collection["targets"]
                    for item in target.get("files", [])}
        self.assertEqual(statuses, {ao.SKIPPED}, collection["targets"])
        self.assertFalse((path / "evidence" / "harness" / "run.json").exists())
        self.assertIn("RESULT: DRY RUN", out)

    def test_dry_run_marks_absence_warnings_as_expected(self):
        """A present agent whose document is not there still warns — but a dry run did not try."""
        path = self.make_folder()
        ao.update_manifest(path, agents={"red": {"present": True}, "blue": {"present": False}})
        code, out = self.run_cli("collect", "demo", RUN_ID, "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING: [dry run", out)
        self.assertRegex(out, r"RED-TEAM\.md is (MISSING|ABSENT)")

    def test_live_collect_announces_the_estate_and_completes_for_a_no_agent_run(self):
        """No agent is present, so every target is local/skipped — no scp, no guest agent."""
        self.make_folder(kind="deploy")
        code, out = self.run_cli("collect", "demo", RUN_ID)
        self.assertEqual(code, 0, out)
        self.assertIn("LIVE COLLECTION", out)
        self.assertIn("reads from the estate", out)
        self.assertIn("RESULT: COMPLETE", out)


class TestArchive(CliTestCase):
    def test_archive_all_lands_files_under_dest(self):
        path = self.settle(self.make_folder(files={"run.json": BUILD_INFO}))
        archive_root = self.child("archive")
        code, out = self.run_cli("archive", "demo", "--all", "--dest", str(archive_root))
        self.assertEqual(code, 0, out)
        dest = archive_root / "demo" / RUN_ID
        self.assertTrue((dest / "REPORT.md").exists())
        self.assertTrue((dest / "collection.json").exists())
        self.assertEqual((dest / "REPORT.md").read_bytes(), (path / "REPORT.md").read_bytes())
        self.assertIn(str(dest), out)

    def test_archive_needs_a_key_or_all(self):
        self.settle(self.make_folder())
        code, out = self.run_cli("archive", "demo")
        self.assertEqual(code, 2, out)
        self.assertIn("exactly one of", out)


class TestUnknownTargets(CliTestCase):
    def test_unknown_comp_exits_nonzero_with_a_message(self):
        for argv in (("list", "nosuchcomp"), ("show", "nosuchcomp", RUN_ID),
                     ("verify", "nosuchcomp", RUN_ID)):
            code, out = self.run_cli(*argv)
            self.assertEqual(code, 2, f"{argv}: {out}")
            self.assertIn("unknown competition", out)

    def test_unknown_key_exits_nonzero_with_a_message(self):
        self.settle(self.make_folder())
        for argv in (("show", "demo", "run-deadbeef"), ("plan", "demo", "run-deadbeef"),
                     ("verify", "demo", "run-deadbeef"), ("archive", "demo", "run-deadbeef")):
            code, out = self.run_cli(*argv)
            self.assertEqual(code, 2, f"{argv}: {out}")
            self.assertIn("no test folder for key", out)
        self.assertIn(RUN_ID, out, "the miss message must name the keys that do exist")

    def test_a_comp_path_with_a_compfile_is_accepted(self):
        path = self.settle(self.make_folder())
        code, out = self.run_cli("show", str(self.comp), RUN_ID)
        self.assertEqual(code, 0, out)
        self.assertIn(str(path), out)


if __name__ == "__main__":
    unittest.main()
