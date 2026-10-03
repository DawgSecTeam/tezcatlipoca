"""Teardown collects this run's test artifacts, and does it before anything is destroyed.

The ordering is the whole feature, not a detail: `destroy_cloned_vms` purges the clones and
`pre_stop_windows_boxes` hard-stops every team box, after which a stopped guest's agent can no
longer answer (destroy-competition.py:487-491). A collector that runs one line later reads
nothing and reports it as "absent", which is indistinguishable from a run that produced no
evidence at all — exactly the ambiguity the artifact folder exists to remove.

Offline: the destructive calls, the placement read, and the frozen check are all patched; only
main()'s own sequencing is under test.
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


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, _REPO / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


destroy = load_module("destroy_competition_for_artifacts_test", "destroy-competition.py")

# Bound before any test patches the module attribute: the patched name is the very function a
# fake would otherwise call to build its base, and the recursion shows up as a RecursionError
# swallowed by the teardown hook's warn-and-proceed handler.
_REAL_DEFAULT_TRANSPORT = ao.default_transport

TEAMS = {"team1": {"identifier": "101", "password": "x"}}
BOXES = [{"name": "dc01", "last_octet": 2, "template": "base-windows-server"}]


class TeardownArtifactsHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        comp = self.root / "competitions" / "demo"
        comp.mkdir(parents=True)
        (comp / "Compfile").write_text("name demo-2026-10-03\nscenario x\ndifficulty 3\n")
        (comp / "teams.json").write_text(json.dumps(TEAMS))
        (comp / "boxes.json").write_text(json.dumps(BOXES))
        (comp / ".deploy_state.json").write_text(json.dumps(
            {"run_id": "run-1a2b3c4d", "scoring_vm_id": 1000}))
        # destroy_cloned_vms only runs when the clone map exists, and it is the first
        # destructive call — the ordering assertion needs it present.
        (comp / "cloned_vms.json").write_text(json.dumps({"101-dc01": 1220}))
        self.comp = comp
        self.log = []
        self._cwd = os.getcwd()

    def _main(self, argv, *, collect=None, cancel=False):
        """Run main() with every destructive call replaced by a log entry."""
        calls = self.log
        collect = collect or (lambda *a, **k: calls.append("collect") or {})
        argv = ["destroy-competition.py", "--competition", "demo"] + argv
        patches = [
            patch.object(destroy, "load_destroyable_competitions", return_value=["demo"]),
            patch.object(destroy, "frozen_state", return_value={}),
            patch.object(destroy, "read_placement", return_value=None),
            patch.object(destroy, "activate_placement"),
            patch.object(destroy, "destroy_cloned_vms",
                         side_effect=lambda *a, **k: calls.append("destroy_cloned_vms")),
            patch.object(destroy, "pre_stop_windows_boxes",
                         side_effect=lambda *a, **k: calls.append("pre_stop_windows_boxes")),
            patch.object(destroy, "destroy_with_recovery",
                         side_effect=lambda *a, **k: calls.append("terraform_destroy") or True),
            patch.object(destroy.artifacts_ops, "collect_for_teardown", side_effect=collect),
            patch.object(sys, "argv", argv),
        ]
        if cancel:
            patches.append(patch("builtins.input", return_value="definitely-not-demo"))
        os.chdir(self.root)
        try:
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                with contextlib.redirect_stdout(io.StringIO()):
                    destroy.main()
        finally:
            os.chdir(self._cwd)
        return calls

    def test_collection_runs_before_any_destructive_call(self):
        calls = self._main(["--yes"])
        self.assertEqual(calls, ["collect", "destroy_cloned_vms", "pre_stop_windows_boxes",
                                 "terraform_destroy"])

    def test_collection_never_runs_when_the_operator_cancels(self):
        with self.assertRaises(SystemExit):
            self._main([], cancel=True)
        self.assertEqual(self.log, [], "a cancelled teardown must not touch the range at all")

    def test_skip_artifacts_flag_bypasses_the_collector(self):
        calls = self._main(["--yes", "--skip-artifacts"])
        self.assertNotIn("collect", calls)
        self.assertIn("terraform_destroy", calls)

    def test_a_collection_crash_does_not_stop_the_teardown(self):
        def boom(*a, **k):
            self.log.append("collect")
            raise RuntimeError("estate on fire")

        calls = self._main(["--yes"], collect=boom)
        self.assertIn("destroy_cloned_vms", calls)
        self.assertIn("terraform_destroy", calls)

    def test_collector_receives_the_run_id_roster_and_timeout(self):
        seen = {}

        def capture(comp_dir, **kwargs):
            seen["comp_dir"] = comp_dir
            seen.update(kwargs)
            return {}

        self._main(["--yes", "--artifacts-timeout", "7"], collect=capture)
        self.assertEqual(seen["run_id"], "run-1a2b3c4d")
        self.assertEqual(seen["teams"], TEAMS)
        self.assertEqual(seen["boxes"], BOXES)
        self.assertEqual(seen["timeout"], 7)
        self.assertEqual(seen["script"], "destroy-competition.py")
        self.assertEqual(Path(seen["comp_dir"]), Path("competitions/demo"))

    def test_legacy_state_without_a_run_id_still_collects(self):
        (self.comp / ".deploy_state.json").write_text(json.dumps({"scoring_vm_id": 1000}))
        seen = {}
        self._main(["--yes"], collect=lambda comp_dir, **kwargs:
                   seen.update(kwargs) or {})
        self.assertIsNone(seen["run_id"])


class CrashedHarnessRecovery(unittest.TestCase):
    """The headline case: the harness died, so teardown is the only thing left that can save the
    run's reports. The REAL collector runs here — only the transport is faked, because a unit
    test must not dial red01."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        comp = self.root / "competitions" / "demo"
        comp.mkdir(parents=True)
        (comp / "Compfile").write_text("name demo-2026-10-03\nscenario x\ndifficulty 3\n")
        (comp / "teams.json").write_text(json.dumps(TEAMS))
        (comp / "boxes.json").write_text(json.dumps(BOXES))
        (comp / ".deploy_state.json").write_text(json.dumps(
            {"run_id": "run-1a2b3c4d", "scoring_vm_id": 1000}))
        (comp / "cloned_vms.json").write_text(json.dumps({"101-dc01": 1220}))
        self.comp, self.log = comp, []
        self._cwd = os.getcwd()

        # What the harness recorded before it died: identity, a run dir, and a red agent.
        run = self.root / "run"
        (run / "evidence").mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({"competition": "demo", "teams": 1}))
        (run / "monitor.log").write_text("monitor up\n")
        (run / "evidence" / "alerts.jsonl").write_text(
            json.dumps({"kind": "red_llm_restart", "detail": "tunnel died"}) + "\n")
        (run / "INTERACTION.md").write_text(
            "# INTERACTION — run-1a2b3c4d\n\n## Verdict\n\n**GREEN — interaction score 11, all "
            "gates passed.**\n\n## Gates (docs/rehearsal-gates.md)\n\n"
            "| side | gate | value | threshold | verdict |\n|---|---|---|---|---|\n"
            "| red | takedowns | 9 | >= 6 | PASS |\n")
        self.run = run
        path, _ = ao.ensure_test(comp, kind="scrim", script="run-agent-scrim.py", teams=1,
                                 boxes=["dc01"], node="pve")
        self.path = path
        ao.update_manifest(path, agents={
            "red": {"present": True, "ip": "10.0.0.198", "vmid": 999, "node": "pve",
                    "ssh": {"host": "10.0.0.198", "user": "sysadmin", "key": "/tmp/key"}},
            "blue": {"present": False}})
        ao.record_paths(path, run_dir=str(run))
        ao.record_phase(path, "stage_red")

    def _transport(self):
        """Only red01's report exists; everything else is reachable-but-empty."""
        transport = dict(_REAL_DEFAULT_TRANSPORT())

        def scp(ssh, remote, dest, timeout=120):
            dest = Path(dest)
            dest.mkdir(parents=True, exist_ok=True)
            if remote.endswith("report-*.md"):
                report = dest / "report-20261003-0200.md"
                report.write_text("# red after-action\n\nTook the DC at T+22.\n")
                return [report]
            raise FileNotFoundError(remote)

        def cmd(ssh, command, dest_file, timeout=90):
            raise FileNotFoundError(command)

        transport["scp-jump"] = scp
        transport["ssh-cmd"] = cmd
        return transport

    def _teardown(self, transport=None):
        calls = self.log
        transport = transport or self._transport()
        patches = [
            patch.object(destroy, "load_destroyable_competitions", return_value=["demo"]),
            patch.object(destroy, "frozen_state", return_value={}),
            patch.object(destroy, "read_placement", return_value=None),
            patch.object(destroy, "activate_placement"),
            patch.object(destroy, "destroy_cloned_vms",
                         side_effect=lambda *a, **k: calls.append("destroy_cloned_vms")),
            patch.object(destroy, "pre_stop_windows_boxes",
                         side_effect=lambda *a, **k: calls.append("pre_stop_windows_boxes")),
            patch.object(destroy, "destroy_with_recovery",
                         side_effect=lambda *a, **k: calls.append("terraform_destroy") or True),
            patch.object(destroy.artifacts_ops, "default_transport",
                         side_effect=lambda: transport),
            patch.object(sys, "argv", ["destroy-competition.py", "--competition", "demo",
                                       "--yes"]),
        ]
        os.chdir(self.root)
        try:
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                with contextlib.redirect_stdout(io.StringIO()):
                    destroy.main()
        finally:
            os.chdir(self._cwd)

    def test_the_red_report_survives_a_dead_harness(self):
        self._teardown()
        self.assertEqual((self.path / "RED-TEAM.md").read_text(),
                         "# red after-action\n\nTook the DC at T+22.\n")
        self.assertTrue((self.path / "evidence" / "red" / "report-20261003-0200.md").exists())

    def test_the_folder_is_complete_and_the_verdict_is_folded_in(self):
        self._teardown()
        manifest = ao.load_manifest(self.path)
        self.assertEqual(manifest["verdict"]["status"], "GREEN")
        report = (self.path / "REPORT.md").read_text()
        self.assertIn("**GREEN**", report)
        self.assertIn("## Recommendations — tezcatlipoca", report)
        self.assertIn("TODO(author)", report)
        self.assertEqual(manifest["teardown"]["by"], "destroy-competition.py")
        self.assertEqual(ao.load_collection(self.path)["summary"]["unreachable"], 0)
        self.assertTrue(any(row["key"] == "run-1a2b3c4d" for row in ao.list_tests(self.comp)))

    def test_the_destroy_still_happens_afterwards(self):
        self._teardown()
        self.assertEqual(self.log, ["destroy_cloned_vms", "pre_stop_windows_boxes",
                                    "terraform_destroy"])

    def test_an_unreachable_red01_leaves_an_honest_stub_not_a_lie(self):
        def dead(ssh, remote, dest, timeout=120):
            raise ao.Unreachable("host is down")

        transport = self._transport()
        transport["scp-jump"] = dead
        self._teardown(transport=transport)
        text = (self.path / "RED-TEAM.md").read_text()
        self.assertIn("status: unreachable", text)
        report = (self.path / "REPORT.md").read_text()
        self.assertIn("RED-TEAM.md", report)  # named in the caveats/warnings, not silently absent


if __name__ == "__main__":
    unittest.main()
