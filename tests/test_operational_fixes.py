"""Owner-requested operational fixes.

- Another run's templates are never destroyed and never read as a clean teardown.
- Golden plant checkpoints are run-scoped, and --replant-goldens clears them.
- Box logs are captured (guest agent) before anything destructive runs.
- The Windows trail wipe always clears the event logs.
- `.env` and the profile resolve from the repo root, not the working directory.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))


class TemplateOwnership(unittest.TestCase):
    def test_another_runs_template_is_blocking_and_untouched(self):
        from golden.golden_ops import OTHER_RUN
        from teardown import machinery
        with mock.patch.object(
                machinery, "destroy_golden_set",
                return_value=[f"{OTHER_RUN} holds vmid 1460 (golden-0, tags: comp-c;run-other)"]
        ), mock.patch.object(machinery, "destroy_engine_template", return_value=[]), \
                mock.patch.object(machinery, "config"), \
                tempfile.TemporaryDirectory() as tmp:
            skipped, blocking = machinery.teardown_templates(
                Path(tmp), {"scoring_vm_id": 1300},
                mock.Mock(tags={"tezcatlipoca", "comp-c", "run-mine"},
                          with_role=lambda *a: mock.Mock(tags={"tezcatlipoca"})),
                [], None, full=True)
        self.assertEqual(skipped, [])
        self.assertEqual(len(blocking), 1)
        self.assertIn("run-other", blocking[0])

    def test_a_foreign_template_stays_a_note(self):
        from teardown import machinery
        with mock.patch.object(machinery, "destroy_golden_set",
                               return_value=["vmid 1460 is occupied by a foreign VM"]), \
                mock.patch.object(machinery, "destroy_engine_template", return_value=[]), \
                mock.patch.object(machinery, "config"), \
                tempfile.TemporaryDirectory() as tmp:
            skipped, blocking = machinery.teardown_templates(
                Path(tmp), {"scoring_vm_id": 1300},
                mock.Mock(tags={"tezcatlipoca"}, with_role=lambda *a: mock.Mock(tags={"x"})),
                [], None, full=True)
        self.assertEqual(blocking, [])
        self.assertEqual(len(skipped), 1)


class GoldenCheckpoints(unittest.TestCase):
    def test_replant_clears_only_the_checkpoints(self):
        from engine import template_freeze
        with tempfile.TemporaryDirectory() as tmp:
            comp = Path(tmp)
            template_freeze.save_template_hashes(
                comp, golden={"box0": "g"}, golden_planted={"box0": {"hash": "h", "run": "r"}})
            template_freeze.clear_golden_plant_checkpoints(comp)
            record = template_freeze.load_template_hashes(comp)
        self.assertNotIn("golden_planted", record)
        self.assertEqual(record.get("golden"), {"box0": "g"})

    def test_the_cli_exposes_the_flags(self):
        from deploy_lib import cli
        parser = cli.build_parser()
        args = parser.parse_args(["--comp-dir", "/tmp/c", "--replant-goldens"])
        self.assertTrue(args.replant_goldens)


class BoxLogs(unittest.TestCase):
    def _ctx(self, vms, ownership_tags):
        inventory = mock.Mock()
        inventory.owned.return_value = [("pve", vm) for vm in vms]
        return mock.Mock(inventory=inventory,
                         ownership=mock.Mock(tags=ownership_tags))

    def test_linux_and_windows_logs_are_written(self):
        from teardown import machinery
        vms = [{"vmid": 101, "name": "web01", "status": "running"},
               {"vmid": 102, "name": "dc01", "status": "running"},
               {"vmid": 103, "name": "stopped", "status": "stopped"}]

        def cfg(method, path, **kw):
            return {"data": {"ostype": "win10" if "102" in path else "l26"}}

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(machinery, "proxmox_api", side_effect=cfg), \
                mock.patch("infra.guest_exec.guest_agent_exec_root",
                           return_value=(0, "journal", "")) as linux, \
                mock.patch("infra.guest_exec.guest_agent_exec_windows",
                           return_value=(0, "events", "")) as win, \
                mock.patch("builtins.print"):
            written = machinery.collect_box_logs(self._ctx(vms, {"tezcatlipoca"}), tmp)
            names = sorted(Path(p).name for p in written)
            self.assertEqual(names, ["dc01-102.log", "web01-101.log"])
            self.assertEqual((Path(tmp) / "dc01-102.log").read_text(), "events")
            self.assertEqual((Path(tmp) / "web01-101.log").read_text(), "journal")
        linux.assert_called_once()
        win.assert_called_once()

    def test_an_unreadable_box_is_recorded_not_raised(self):
        from teardown import machinery
        vms = [{"vmid": 101, "name": "web01", "status": "running"}]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(machinery, "proxmox_api",
                                  return_value={"data": {"ostype": "l26"}}), \
                mock.patch("infra.guest_exec.guest_agent_exec_root",
                           side_effect=RuntimeError("agent down")), \
                mock.patch("builtins.print"):
            written = machinery.collect_box_logs(self._ctx(vms, {"tezcatlipoca"}), tmp)
            text = Path(written[0]).read_text()
        self.assertIn("agent down", text)


class WindowsTrailWipe(unittest.TestCase):
    def test_event_logs_are_always_cleared(self):
        from scrim import clean_trails
        self.assertIn("wevtutil cl Security", clean_trails.WINDOWS_PS)
        self.assertNotIn("SCRIM_CLEAR_WIN_EVENTLOGS", clean_trails.WINDOWS_PS)


class AnchoredPaths(unittest.TestCase):
    def test_the_profile_default_is_under_the_repo_root(self):
        from estate import config
        self.assertEqual(Path(config.DEFAULT_PATH), config.REPO_ROOT / "tezcatlipoca.toml")

    def test_a_relative_tez_config_is_anchored(self):
        from estate import config
        with mock.patch.dict("os.environ", {"TEZ_CONFIG": "alt.toml"}):
            cfg = config.Config()
        self.assertEqual(cfg.path, config.REPO_ROOT / "alt.toml")

    def test_driver_env_paths_are_under_the_repo_root(self):
        import deploy
        self.assertEqual(deploy.ENV_PATH, _REPO / ".env")


if __name__ == "__main__":
    unittest.main()

def test_a_redeploy_forgets_the_previous_ranges_teardown_journal(tmp_path):
    from estate import state
    state.clear_teardown_journal(tmp_path)          # nothing to clear: no document written
    document = state.EstateState.load(tmp_path)
    document.data["teardown"] = {"run_id": "run-1", "done": {"artifacts": {"detail": "x"}}}
    document.save()
    state.clear_teardown_journal(tmp_path)
    assert state.EstateState.load(tmp_path).data["teardown"] == {}
