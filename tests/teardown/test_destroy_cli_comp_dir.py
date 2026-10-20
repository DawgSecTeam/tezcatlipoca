"""`destroy-competition.py --comp-dir` on its own must work.

That is the dashboard's shape: its destroy run is handed a per-run workspace path and no
competition NAME (the workspace dir is called `comp`), and the banner prints the name —
`competition` was only bound in the comps-root branch, so the CLI died with UnboundLocalError
before tearing anything down (caught live).
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
# The ambient estate identity every lifecycle entry point refuses without.
IDENTITY = "https://pve.invalid:8006"


def load_destroy_cli(tmp_path):
    """Import the CLI with CWD outside the repo so its `load_dotenv('.env')` finds nothing."""
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("destroy_cli_under_test",
                                                      REPO / "destroy-competition.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(cwd)


def make_workspace(tmp_path):
    comp = tmp_path / "comp"
    comp.mkdir()
    (comp / "Compfile").write_text("name scrim-one\nscenario x\ndifficulty 6\n")
    (comp / "teams.json").write_text(json.dumps({"team1": {"identifier": "120"}}))
    (comp / "boxes.json").write_text(json.dumps([{"name": "web01", "template": "t",
                                                  "last_octet": 4}]))
    return comp


class CompDirOnly(unittest.TestCase):
    def test_comp_dir_without_a_name_reaches_the_teardown(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            comp = make_workspace(tmp_path)
            module = load_destroy_cli(tmp_path)
            seen = {}

            def fake_teardown(comp_dir, teams, boxes, name, **kw):
                seen.update(comp_dir=str(comp_dir), teams=teams, name=name)
                return SimpleNamespace(clean=True, render=lambda: "clean")

            with mock.patch.object(sys, "argv",
                                   ["destroy-competition.py", "--comp-dir", str(comp), "--yes"]), \
                    mock.patch.dict(os.environ, {"TEZ_PROXMOX_ENDPOINT": IDENTITY}), \
                    mock.patch("teardown.runner.run_terminal_teardown",
                               side_effect=fake_teardown):
                module.main()                     # UnboundLocalError here before the fix
            self.assertEqual(seen["comp_dir"], str(comp))
            self.assertEqual(seen["teams"], {"team1": {"identifier": "120"}})



class ConfigLessWorkspace(unittest.TestCase):
    """A workspace whose deploy died before generating teams.json/boxes.json holds no range.

    Reading them unconditionally died with FileNotFoundError instead of a verdict (caught live,
    two runs were handed another run's config-less workspace — one died in
    preflight-placement with a deploy state but no generated competition config)."""

    def test_a_missing_generated_config_is_refused_before_anything_is_planned(self):
        import contextlib
        import io
        import tempfile
        for name in ("teams.json", "boxes.json"):
            with self.subTest(missing=name), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                comp = make_workspace(tmp_path)
                (comp / name).unlink()
                module = load_destroy_cli(tmp_path)
                calls, out = [], io.StringIO()
                with mock.patch.object(sys, "argv",
                                       ["destroy-competition.py", "--comp-dir", str(comp),
                                        "--yes"]), \
                        mock.patch.dict(os.environ, {"TEZ_PROXMOX_ENDPOINT": IDENTITY}), \
                        mock.patch("teardown.runner.run_terminal_teardown",
                                   side_effect=lambda *a, **k: calls.append(a)), \
                        contextlib.redirect_stdout(out):
                    with self.assertRaises(SystemExit) as e:
                        module.main()
                self.assertEqual(e.exception.code, 1)
                self.assertEqual(calls, [], "no teardown may be planned or run")
                self.assertIn(name, out.getvalue())

    def test_a_run_id_workspace_is_torn_down_by_its_recorded_resources(self):
        # A deploy that died before generating teams.json still owns a run id, and its range
        # must stay reclaimable: refusing it left the dashboard range unretireable forever.
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            comp = make_workspace(tmp_path)
            (comp / "teams.json").unlink()
            (comp / "estate-state.json").write_text(json.dumps({
                "schema_version": 1,
                "identity": {"comp_name": "scrim-one", "run_id": "run-abc12345"},
                "resources": [],
            }))
            module = load_destroy_cli(tmp_path)
            seen, out = {}, io.StringIO()

            def fake_teardown(comp_dir, teams, boxes, name, **kw):
                seen.update(teams=teams, boxes=boxes)
                return SimpleNamespace(clean=True, render=lambda: "clean")

            with mock.patch.object(sys, "argv",
                                   ["destroy-competition.py", "--comp-dir", str(comp),
                                    "--yes"]), \
                    mock.patch.dict(os.environ, {"TEZ_PROXMOX_ENDPOINT": IDENTITY}), \
                    mock.patch("teardown.runner.run_terminal_teardown",
                               side_effect=fake_teardown), \
                    contextlib.redirect_stdout(out):
                module.main()
        self.assertEqual(seen["teams"], {})
        self.assertEqual(seen["boxes"], [{"name": "web01", "template": "t",
                                          "last_octet": 4}])
        self.assertIn("never finished generating", out.getvalue())


if __name__ == "__main__":
    unittest.main()
