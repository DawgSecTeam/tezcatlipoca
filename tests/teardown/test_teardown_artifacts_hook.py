"""Teardown collects this run's test artifacts, and does it before anything is destroyed.

The ordering is the whole feature: a collector that runs one line later reads nothing and
reports "absent", indistinguishable from a run that produced no evidence. Offline — the
engine's destructive steps are patched at their owning modules and the Proxmox API is a fake.
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

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from artifacts_lib import artifacts_ops as ao  # noqa: E402
from red import red01_ops, tlaloc_ops  # noqa: E402
import infra.client  # noqa: E402
from infra import ownership  # noqa: E402
from artifacts_lib import lifecycle as alifecycle  # noqa: E402
from artifacts_lib import paths as apaths  # noqa: E402
from estate import state as estate_state  # noqa: E402
from teardown import machinery as tm  # noqa: E402


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, _REPO / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


destroy = load_module("destroy_competition_for_artifacts_test", "destroy-competition.py")

# Bound before any test patches the module attribute: the patched name is the very function a
# fake would otherwise call to build its base, and the recursion surfaces as a RecursionError
# swallowed by the teardown hook's warn-and-proceed handler.
_REAL_DEFAULT_TRANSPORT = ao.default_transport

TEAMS = {"team1": {"identifier": "101", "password": "x"}}
BOXES = [{"name": "dc01", "last_octet": 2, "template": "base-windows-server"}]


class _OfflinePVE:
# Empty estate: every node lists clean, nothing is running.

    def __call__(self, method, path, **kw):
        return {"data": []}


def _offline_api_patches(collect_log=None):
    # Every module that can reach Proxmox, patched at its owner: a partial patch sends the
    #     other half LIVE and the test dials the node.
    patches = [
        patch.object(infra.client, "proxmox_api", _OfflinePVE()),
        patch.object(ownership, "proxmox_api", _OfflinePVE()),
        patch.object(ownership, "wait_for_proxmox_task", lambda *a, **k: None),
        patch.object(infra.client, "live_vmids", lambda: []),
        # red01.py binds proxmox_api itself; an unpatched binding sent the red01 verdict
        # check LIVE and read back "unknown", which now (correctly) fails the teardown.
        patch("infra.red01.proxmox_api", _OfflinePVE()),
        patch.object(red01_ops, "recorded_identity",
                     lambda *a, **k: {"vmid": 999, "ip": "", "node": ""}),      # known vmid; live_vmids [] proves it gone
    ]
    return patches


def _collecting_fake(calls):
    # A stand-in that creates the folder it claims to — verify_clean demands the evidence dir
    # exist when the step is journalled.
    def fake(comp_dir, **kwargs):
        calls.append("collect")
        key, _ = alifecycle.latest_run_key(comp_dir, kwargs.get("run_id"))
        Path(apaths.test_dir(comp_dir, key)).mkdir(parents=True, exist_ok=True)
        return {}
    return fake


class TeardownArtifactsHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        comp = self.root / "competitions" / "demo"
        comp.mkdir(parents=True)
        (comp / "Compfile").write_text("name demo-comp\nscenario x\ndifficulty 3\n")
        (comp / "teams.json").write_text(json.dumps(TEAMS))
        (comp / "boxes.json").write_text(json.dumps(BOXES))
        document = estate_state.EstateState.load(comp)
        document.absorb({"run_id": "run-1a2b3c4d", "scoring_vm_id": 1000})
        document.save()
        self.comp = comp
        self.log = []
        self._cwd = os.getcwd()

    def _main(self, argv, *, collect=None, red=None, estate=None, cancel=False):
    # Run main() with every destructive call replaced by a log entry.
        calls = self.log
        collect = collect or _collecting_fake(calls)
        red = red or (lambda *a, **k: calls.append("red_destroy") or
                      __import__("subprocess").CompletedProcess(["x"], 0, "", ""))
        estate = estate if estate is not None else (
            lambda *a, **k: calls.append("estate_destroy") or
            {"destroyed": [], "skipped": [], "refused": [], "kept": []})
        argv = ["destroy-competition.py", "--competition", "demo"] + argv
        patches = [
            patch.object(destroy, "load_destroyable_competitions", return_value=["demo"]),
            patch.object(destroy, "frozen_state", return_value={}),
            patch.object(tm, "pre_stop_windows_boxes",
                         side_effect=lambda *a, **k: calls.append("pre_stop_windows_boxes")),
            patch.object(tm, "sweep_tagged_leftovers"),
            patch.object(tm, "teardown_templates", return_value=[]),
            patch("estate.destroy.destroy_estate", side_effect=estate),
            patch.object(ao, "collect_for_teardown", side_effect=collect),
            patch.object(tlaloc_ops, "destroy_tlaloc_red", side_effect=red),
            ] + _offline_api_patches() + [
            # The entry point refuses without the ambient estate identity (verify_clean needs it);
            # this harness tests the hook, not the refusal.
            patch.dict(os.environ, {"TEZ_PROXMOX_ENDPOINT": "https://pve.invalid:8006"}),
            patch.object(sys, "argv", argv),
        ]
        if cancel:
            patches.append(patch("builtins.input", return_value="definitely-not-demo"))
        os.chdir(self.root)
        buf = io.StringIO()
        try:
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                with contextlib.redirect_stdout(buf):
                    destroy.main()
        finally:
            os.chdir(self._cwd)
            self.out = buf.getvalue()
        return calls

    def test_collection_runs_before_any_destructive_call(self):
        calls = self._main(["--yes"])
        self.assertEqual(calls, ["collect", "red_destroy", "pre_stop_windows_boxes",
                                 "estate_destroy"])

    def test_collection_never_runs_when_the_operator_cancels(self):
        with self.assertRaises(SystemExit):
            self._main([], cancel=True)
        self.assertEqual(self.log, [], "a cancelled teardown must not touch the range at all")


    def test_a_collection_crash_does_not_stop_the_teardown(self):
        def boom(*a, **k):
            self.log.append("collect")
            raise RuntimeError("estate on fire")

        # The destroy is NOT blocked — but the exit contract is honest: a crashed collection means
    # the evidence was not archived.
        with self.assertRaises(SystemExit) as ctx:
            self._main(["--yes"], collect=boom)
        self.assertTrue(ctx.exception.code)
        self.assertIn("pre_stop_windows_boxes", self.log)
        self.assertIn("estate_destroy", self.log)
        self.assertIn("evidence was not archived", self.out)


    def test_state_without_a_run_id_refuses_before_collecting_or_destroying(self):
        document = estate_state.EstateState.load(self.comp)
        document.absorb({"scoring_vm_id": 1000})
        document.data["identity"].pop("run_id", None)
        document.save()
        with self.assertRaises(SystemExit) as ctx:
            self._main(["--yes"])
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("no run id", self.out)
        self.assertEqual(self.log, [])



class CrashedHarnessRecovery(unittest.TestCase):
    # The headline case: a dead harness, so teardown is the only thing left that can save the
    #     run's reports. The REAL collector runs — only the transport is faked.

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        comp = self.root / "competitions" / "demo"
        comp.mkdir(parents=True)
        (comp / "Compfile").write_text("name demo-comp\nscenario x\ndifficulty 3\n")
        (comp / "teams.json").write_text(json.dumps(TEAMS))
        (comp / "boxes.json").write_text(json.dumps(BOXES))
        document = estate_state.EstateState.load(comp)
        document.absorb({"run_id": "run-1a2b3c4d", "scoring_vm_id": 1000})
        document.save()
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
        # Only red01's report exists; everything else is reachable-but-empty.
        transport = dict(_REAL_DEFAULT_TRANSPORT())

        def scp(ssh, remote, dest, timeout=120):
            dest = Path(dest)
            dest.mkdir(parents=True, exist_ok=True)
            if remote.endswith("report.md"):
                report = dest / "report.md"
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
            patch.object(tm, "pre_stop_windows_boxes",
                         side_effect=lambda *a, **k: calls.append("pre_stop_windows_boxes")),
            patch.object(tm, "sweep_tagged_leftovers"),
            patch.object(tm, "teardown_templates", return_value=[]),
            patch("estate.destroy.destroy_estate",
                  side_effect=lambda *a, **k: calls.append("estate_destroy") or
                  {"destroyed": [], "skipped": [], "refused": [], "kept": []}),
            patch.object(ao, "default_transport", side_effect=lambda: transport),
            # stage_red_artifacts shells out to ssh OUTSIDE the transport dict; a test must not dial
            # the manifest's IP (it blackholes for the full 75s subprocess timeout).
            patch("artifacts_lib.lifecycle.stage_red_artifacts",
                  side_effect=lambda manifest: calls.append("stage_red")),
            patch.object(tlaloc_ops, "destroy_tlaloc_red",
                         side_effect=lambda *a, **k: calls.append("red_destroy") or
                         __import__("subprocess").CompletedProcess(["x"], 0, "", "")),
            ] + _offline_api_patches() + [
            # The entry point refuses without the ambient estate identity (verify_clean needs it);
            # this harness tests recovery, not the refusal.
            patch.dict(os.environ, {"TEZ_PROXMOX_ENDPOINT": "https://pve.invalid:8006"}),
            patch.object(sys, "argv", ["destroy-competition.py", "--competition", "demo",
                                       "--yes"]),
        ]
        os.chdir(self.root)
        buf = io.StringIO()
        try:
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                with contextlib.redirect_stdout(buf):
                    destroy.main()
        finally:
            os.chdir(self._cwd)
            self.out = buf.getvalue()

    def test_the_red_report_survives_a_dead_harness(self):
        self._teardown()
        self.assertEqual((self.path / "RED-TEAM.md").read_text(),
                         "# red after-action\n\nTook the DC at T+22.\n")
        self.assertTrue((self.path / "evidence" / "red" / "report.md").exists())



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
