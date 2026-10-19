"""test-artifacts.py CLI: the read/inspect/repair surface over `.automated-tests/<key>/`."""

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

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from artifacts_lib import artifacts_ops as ao
from estate import state as estate_state

RUN_ID = "run-1a2b3c4d"
BUILD_INFO = '{"ok": true}\n'


def _load_cli():
    spec = importlib.util.spec_from_file_location("test_artifacts_cli_under_test",
                                                  _REPO / "test-artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.comp = self.root / "competitions" / "demo"
        self.comp.mkdir(parents=True)
        (self.comp / "Compfile").write_text("name demo-comp\nscenario demo\n")
        estate_state.write_flat(self.comp,
                                {"run_id": RUN_ID, "teams": {"team1": {"identifier": "101"}}})
        self._cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._cwd)
        # seal_test refreshes the durable archive on a linked worktree; redirect it to stay hermetic.
        patcher = patch.dict(os.environ,
                             {"TEZ_ARTIFACTS_ARCHIVE": str(self.root / "archive")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key = ao.test_key(self.comp)
        self.assertEqual(self.key, RUN_ID)

    def run_cli(self, *argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = cli.main(list(argv))
        return code, buffer.getvalue()

    def make_folder(self, kind="deploy", *, files=None):
        path, _ = ao.ensure_test(self.comp, kind=kind, script="tests/artifacts/test_test_artifacts_cli.py")
        if files is not None:
            run_dir = self.root / "run"
            run_dir.mkdir(exist_ok=True)
            for name, body in files.items():
                (run_dir / name).write_text(body)
            ao.record_paths(path, run_dir=str(run_dir))
        return path

    def settle(self, path):
    # What teardown leaves behind.
        ao.collect(path, ao.plan_targets(ao.load_manifest(path), comp_dir=self.comp))
        ao.finalize(path, manifest=ao.load_manifest(path))
        return path

    def child(self, *parts):
        return self.root.joinpath(*parts)


class TestList(CliTestCase):

    def test_json_rows_are_machine_readable(self):
        self.settle(self.make_folder())
        code, out = self.run_cli("list", "demo", "--json")
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertEqual([row["key"] for row in payload["tests"]], [RUN_ID])
        self.assertEqual(payload["tests"][0]["kind"], "deploy")




class TestShow(CliTestCase):


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



if __name__ == "__main__":
    unittest.main()
