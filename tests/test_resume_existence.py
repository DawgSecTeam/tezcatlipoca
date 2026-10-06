"""Resume existence gates + checkpoint truth (C1) — scale8 soak 2026-10-02.

Attempts 9-11 of that soak resumed at `--from-phase 5/6` after a phase-4 death. Apply
#2 had never run, so no team box existed; the phase-5 repair sweep is `strict=False`,
so it "succeeded" against 32 nonexistent machines and wrote `last_phase=5`. The next
resume read that stamp as proof the machines were there, every downstream wait raced a
ghost, and the run spent ~40 minutes in `wait_for_windows_sshd` before anyone worked out
why. `guard_resume_from_phase` could not catch it: it reads the checkpoint, and the
checkpoint was the lie.

These tests pin the three answers that fix it:
  - `team_vmids_from_state` parses the one artifact that records what was built, and
    refuses to report "nothing there" when it simply could not read it;
  - `guard_resume_existence` refuses a resume whose skipped phases left nothing behind,
    and names the phase to actually use;
  - `DeployContext.checkpoint(n)` will not stamp a claim the range cannot back.

Offline: no Proxmox, no network. The terraform-facing test runs a fake `terraform` on
PATH (the pattern tests/test_run_terraform.py proved), so the real subprocess wrapper,
argument list and stderr/stdout handling are all exercised.
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import deploy  # noqa: E402
from _deploy_patch import dpatch  # noqa: E402
from deploy_lib import gates as dl_gates  # noqa: E402
import range_ops  # noqa: E402

STATE_RESOURCES = [
    {"type": "proxmox_virtual_environment_vm", "name": "scoring_engine",
     "instances": [{"attributes": {"id": "200", "vm_id": 200}}]},
    {"type": "proxmox_virtual_environment_vm", "name": "team_box",
     "instances": [{"attributes": {"id": "221", "vm_id": 221}},
                   {"attributes": {"id": "222", "vm_id": 222}}]},
    {"type": "proxmox_virtual_environment_vm", "name": "team_box_sat1",
     "instances": [{"attributes": {"id": "225", "vm_id": 225}}]},
    {"type": "null_resource", "name": "team_nics",
     "instances": [{"attributes": {"id": "221"}}]},
    {"type": "proxmox_network_linux_bridge", "name": "team_bridge",
     "instances": [{"attributes": {"id": "221"}}]},
]
STATE_JSON = json.dumps({"version": 4, "serial": 14, "resources": STATE_RESOURCES})

# The real team_box address keys are BOX NAMES, not vmids — the vmid lives in the
# instance attributes (live-found 2026-10-03: an address-shape parser read a fully
# built range as "no team boxes at all" and failed the phase-4 checkpoint).


def _fake_terraform(tmp, stdout=STATE_JSON, returncode=0, stderr=""):
    """A `terraform` shim on PATH that answers `state pull` and nothing else."""
    bindir = Path(tmp) / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "terraform"
    script.write_text(
        "#!/bin/sh\n"
        f"cat <<'EOF'\n{stdout}\nEOF\n"
        f"echo {json.dumps(stderr)} >&2\n"
        f"exit {returncode}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bindir


class TerraformStateReading(unittest.TestCase):
    """`terraform state pull` -> the vmids a completed phase actually created."""

    def _comp(self, tmp):
        comp = Path(tmp) / "competitions" / "c1"
        (comp / "terraform").mkdir(parents=True)
        return comp

    def test_parses_vm_attributes_and_keeps_satellite_boxes(self):
        with tempfile.TemporaryDirectory() as d:
            comp = self._comp(d)
            bindir = _fake_terraform(d)
            with patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
                vmids = range_ops.team_vmids_from_state(comp)
        # The engine is read through state["scoring_vm_id"], not this parser, and the
        # null_resource/bridge entries must not be mistaken for machines.
        self.assertEqual(vmids, [221, 222, 225])

    def test_team_box_without_vmid_attribute_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            comp = self._comp(d)
            resources = json.loads(json.dumps(STATE_RESOURCES))
            resources[-3]["instances"][0]["attributes"] = {}  # sat1 lost its vm_id
            bindir = _fake_terraform(d, stdout=json.dumps({"resources": resources}))
            with patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
                self.assertEqual(range_ops.team_vmids_from_state(comp), [221, 222])

    def test_unreadable_state_raises_instead_of_reporting_nothing(self):
        # The distinction the whole gate rests on: "no machines" and "could not ask"
        # must not look alike, or the gate hard-fails a healthy range.
        with tempfile.TemporaryDirectory() as d:
            comp = self._comp(d)
            bindir = _fake_terraform(d, returncode=1, stderr="Error: no state file")
            with patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
                with self.assertRaises(RuntimeError) as raised:
                    range_ops.team_vmids_from_state(comp)
        self.assertIn("state pull", str(raised.exception))

    def test_unparsable_state_raises_instead_of_reporting_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            comp = self._comp(d)
            bindir = _fake_terraform(d, stdout="not json at all")
            with patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
                with self.assertRaises(RuntimeError) as raised:
                    range_ops.team_vmids_from_state(comp)
        self.assertIn("unparsable", str(raised.exception))

    def test_missing_terraform_binary_raises(self):
        with tempfile.TemporaryDirectory() as d:
            comp = self._comp(d)
            with patch.dict(os.environ, {"PATH": str(Path(d) / "empty")}):
                with self.assertRaises(RuntimeError):
                    range_ops.team_vmids_from_state(comp)

    def test_empty_state_is_an_empty_list_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            comp = self._comp(d)
            bindir = _fake_terraform(d, stdout="{}")
            with patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
                self.assertEqual(range_ops.team_vmids_from_state(comp), [])


class ResumeExistenceGuard(unittest.TestCase):
    """The gate itself, with every probe injected (no Proxmox)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.comp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _state(self, **kw):
        return {"scoring_vm_id": 1900, **kw}

    def _guard(self, from_phase, *, vmids=(221, 222), live=(221, 222, 1900),
               reachable=(True, ""), state=None, **kw):
        dl_gates.guard_resume_existence(
            from_phase, self.comp, self._state(**(state or {})),
            vmids_from_state=lambda: list(vmids),
            vm_exists=lambda: set(live),
            box_reachable=lambda *a: reachable,
            **kw)

    def test_early_resume_needs_nothing(self):
        # --from-phase 2 rebuilds the engine itself; there is nothing to verify.
        self._guard(2, vmids=(), live=())

    def test_resuming_past_the_engine_requires_a_live_engine(self):
        with self.assertRaises(SystemExit) as raised:
            self._guard(3, vmids=(), live=())
        self.assertIn("1900", str(raised.exception))
        self.assertIn("--from-phase 2", str(raised.exception))

    def test_engine_present_but_boxes_missing_is_refused(self):
        # The soak's exact shape: 32 machines recorded, none of them real.
        with self.assertRaises(SystemExit) as raised:
            self._guard(5, vmids=(221, 222), live=(1900,))
        msg = str(raised.exception)
        self.assertIn("2 of 2", msg)
        self.assertIn("--from-phase 4", msg)

    def test_no_boxes_recorded_at_all_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            self._guard(5, vmids=(), live=(1900,))
        self.assertIn("no team boxes at all", str(raised.exception))

    def test_a_few_missing_boxes_still_refuses(self):
        # Partial infrastructure is not a resume target: the phases downstream address
        # every box by computed vmid.
        with self.assertRaises(SystemExit):
            self._guard(5, vmids=(221, 222, 225), live=(1900, 221, 222))

    def test_boxes_present_allows_phase_5(self):
        self._guard(5)

    def test_phase_6_also_requires_the_sweep_and_a_live_box(self):
        with self.assertRaises(SystemExit) as raised:
            self._guard(6, reachable=(False, ".postclone-swept is absent"))
        msg = str(raised.exception)
        self.assertIn("postclone-swept", msg)
        self.assertIn("--from-phase 5", msg)

    def test_phase_6_passes_when_the_sweep_and_a_box_are_there(self):
        self._guard(6)

    def test_unreadable_state_refuses_rather_than_resuming_blind(self):
        def boom():
            raise RuntimeError("could not run `terraform state list`")
        with self.assertRaises(SystemExit) as raised:
            dl_gates.guard_resume_existence(
                5, self.comp, self._state(), vmids_from_state=boom,
                vm_exists=lambda: {1900}, box_reachable=lambda *a: (True, ""))
        self.assertIn("cannot be verified", str(raised.exception))

    def test_unreachable_api_refuses_rather_than_calling_everything_gone(self):
        def boom():
            raise RuntimeError("could not list the cluster's VMs")
        with self.assertRaises(SystemExit) as raised:
            dl_gates.guard_resume_existence(
                3, self.comp, self._state(), vmids_from_state=lambda: [],
                vm_exists=boom, box_reachable=lambda *a: (True, ""))
        self.assertIn("Refusing to resume blind", str(raised.exception))

    def test_force_from_phase_still_escalates(self):
        # The operator escape hatch survives: what C1 removes is the SILENT skip.
        self._guard(6, vmids=(), live=(), force=True)

    def test_probe_defaults_come_from_the_artifact_probe_seam(self):
        # The seam that makes this testable must be the one production uses.
        calls = []
        with dpatch("_artifact_probes", lambda comp_dir: {
                "vmids_from_state": lambda: calls.append("state") or [221],
                "vm_exists": lambda: calls.append("live") or {1900, 221},
                "box_reachable": lambda *a: (True, "")}):
            dl_gates.guard_resume_existence(5, self.comp, self._state())
        self.assertEqual(calls, ["live", "state"])


class SweepMarkerAndBoxProbe(unittest.TestCase):
    """`_any_box_reachable`: the marker proves the sweep ran, SSH proves boxes are real."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.comp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_missing_marker_is_reported_by_name(self):
        ok, why = dl_gates._any_box_reachable(self.comp, {})
        self.assertFalse(ok)
        self.assertIn(".postclone-swept", why)

    def test_marker_plus_a_responding_box_passes(self):
        (self.comp / ".postclone-swept").write_text("")
        (self.comp / "targets.json").write_text(json.dumps(
            {"targets": {"team1-web01": {"ip": "192.168.221.4"}}}))
        with patch("ssh_ops.read_terraform_ctx", return_value={"ssh_key_path": "/k"}), \
                patch("ssh_ops.ssh_via_gateway",
                      return_value=type("P", (), {"stdout": "RESUME-OK\n"})()):
            ok, why = dl_gates._any_box_reachable(self.comp, {})
        self.assertTrue(ok, why)

    def test_marker_but_no_box_answering_is_refused(self):
        (self.comp / ".postclone-swept").write_text("")
        (self.comp / "targets.json").write_text(json.dumps(
            {"targets": {"team1-web01": {"ip": "192.168.221.4"}}}))
        with patch("ssh_ops.read_terraform_ctx", return_value={"ssh_key_path": "/k"}), \
                patch("ssh_ops.ssh_via_gateway", side_effect=OSError("no route")):
            ok, why = dl_gates._any_box_reachable(self.comp, {})
        self.assertFalse(ok)
        self.assertIn("answered over SSH", why)

    def test_engine_box_is_not_accepted_as_evidence(self):
        # targets.json includes the engine; a range whose only reachable machine is the
        # engine is exactly the state this gate exists to catch.
        (self.comp / ".postclone-swept").write_text("")
        (self.comp / "targets.json").write_text(json.dumps(
            {"targets": {"scoring-engine": {"ip": "10.0.0.252"}}}))
        ok, why = dl_gates._any_box_reachable(self.comp, {})
        self.assertFalse(ok)
        self.assertIn("no team box addresses", why)


class CheckpointTruth(unittest.TestCase):
    """`checkpoint(n)` must not record a phase whose output is not there.

    This is the defense-in-depth half: even with the resume gate in place, a run must
    never write a checkpoint that sends the NEXT resume chasing ghosts."""

    def _ctx(self, comp_dir, state):
        return deploy.DeployContext(
            comp_dir=comp_dir, comp_name=comp_dir.name,
            state_path=comp_dir / ".deploy_state.json", from_phase=1, resuming=False,
            assume_yes=True, name="probe", scenario="probe", box_username="ubuntu",
            credlist_usernames=["admin"], nakon_jobs=4, apt_cache=True, injects=[],
            packet_pw=None, state=state, teams={}, number_of_teams=0,
            admin_password="a", scoring_password="s", postgres_password="p",
            redis_password="r", box_password="b", box_creds={}, domain_creds=None,
            inject_password=None, placement=None, node="pve", engine_vmid=1900,
            engine_mgmt_ip="10.0.0.1", boxes=[], boxes_by_name={}, unbooted=set(),
            nakon_config_path=comp_dir / "nakon.json",
            golden_config_path=comp_dir / "golden.json",
            repair_config_path=comp_dir / "repair.json",
            final_config_path=comp_dir / "final.json",
            golden_inputs={}, golden_hashes={}, frozen_keep=set(),
            tf_dir=comp_dir, tfvars_path=comp_dir / "tfvars.json", tfvars={},
            ssh_key_abs="/k", all_targets=[], managed_targets=[], linux_targets=[],
            windows_targets=[])

    def test_phase_3_refuses_to_stamp_when_the_engine_is_gone(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = self._ctx(Path(d), {"scoring_vm_id": 1900})
            with dpatch("_artifact_probes", lambda c: {
                    "vmids_from_state": lambda: [],
                    "vm_exists": lambda: set(),
                    "box_reachable": lambda *a: (True, "")}):
                with self.assertRaises(SystemExit) as raised:
                    ctx.checkpoint(3)
            self.assertIn("last_phase=3 will NOT be recorded", str(raised.exception))
            self.assertIsNone(ctx.state.get("last_phase"))

    def test_phase_4_refuses_to_stamp_when_the_boxes_are_gone(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = self._ctx(Path(d), {"scoring_vm_id": 1900})
            with dpatch("_artifact_probes", lambda c: {
                    "vmids_from_state": lambda: [221, 222],
                    "vm_exists": lambda: {1900},
                    "box_reachable": lambda *a: (True, "")}):
                with self.assertRaises(SystemExit):
                    ctx.checkpoint(4)

    def test_phase_4_stamps_when_the_boxes_are_there(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = self._ctx(Path(d), {"scoring_vm_id": 1900})
            with dpatch("_artifact_probes", lambda c: {
                    "vmids_from_state": lambda: [221, 222],
                    "vm_exists": lambda: {1900, 221, 222},
                    "box_reachable": lambda *a: (True, "")}):
                ctx.checkpoint(4)
        self.assertEqual(ctx.state["last_phase"], 4)

    def test_ungated_phases_still_stamp_without_touching_proxmox(self):
        # Phases 1, 2, 5, 6, 7 build nothing independently checkable; gating them would
        # refuse good deploys, so they must not even consult the probes.
        with tempfile.TemporaryDirectory() as d:
            for n in (1, 2, 5, 6, 7):
                ctx = self._ctx(Path(d), {})
                with dpatch("_artifact_probes",
                                  side_effect=AssertionError("must not probe")):
                    ctx.checkpoint(n)
                self.assertEqual(ctx.state["last_phase"], n)


if __name__ == "__main__":
    unittest.main()
