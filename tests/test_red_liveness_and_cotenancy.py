"""Diagnostics a red-side post-mortem actually needs.

Three things a live run proved missing on 2026-10-15 (run-1c44733b, the official-DeepSeek scrim):
a red01 outage that could not be attributed to the box or the route, a verify FAIL whose only
record was a one-line summary, and a co-tenanted node nobody mentioned in the run log.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from artifacts_lib import plan
from scrim import fire_test, red_link, staging


class RedBoxPowerState(unittest.TestCase):

    def test_reports_the_hypervisor_state_not_a_guest_probe(self):
        args = SimpleNamespace(red_vmid=999)
        with mock.patch.dict(os.environ, {"TF_VAR_proxmox_node": "proxmox"}), \
             mock.patch("infra.client.proxmox_api", return_value={"data": {"status": "running"}}):
            self.assertEqual(red_link.red_box_power_state(args), "vmid 999 is running (Proxmox)")

    def test_a_destroyed_vm_says_so(self):
        args = SimpleNamespace(red_vmid=999)
        with mock.patch.dict(os.environ, {"TF_VAR_proxmox_node": "proxmox"}), \
             mock.patch("infra.client.proxmox_api", return_value={"data": {}}):
            self.assertIn("unknown", red_link.red_box_power_state(args))

    def test_an_unreadable_api_is_reported_not_raised(self):
        args = SimpleNamespace(red_vmid=999)
        with mock.patch.dict(os.environ, {"TF_VAR_proxmox_node": "proxmox"}), \
             mock.patch("infra.client.proxmox_api", side_effect=RuntimeError("no route")):
            self.assertIn("unreadable from Proxmox (RuntimeError)",
                          red_link.red_box_power_state(args))

    def test_no_vmid_is_unknown_rather_than_a_guess(self):
        self.assertIsNone(red_link.red_box_power_state(SimpleNamespace(red_vmid=None)))


class VerifyLog(unittest.TestCase):

    def test_the_whole_output_lands_in_the_run_folder_with_its_label(self):
        r = SimpleNamespace(returncode=1, stdout="gate: PASS\n'misconfig' absent on every team",
                            stderr="boom")
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(run_dir=tmp)
            with mock.patch.object(fire_test.core, "secure_evidence", lambda p: None):
                path = fire_test.write_verify_log(args, "pre-T0 (authoritative)", r)
            text = Path(path).read_text()
        self.assertIn("verify [pre-T0 (authoritative)] rc=1", text)
        self.assertIn("'misconfig' absent on every team", text)
        self.assertIn("=== STDERR ===", text)
        self.assertIn("boom", text)

    def test_no_run_dir_is_not_an_error(self):
        r = SimpleNamespace(returncode=0, stdout="", stderr="")
        self.assertIsNone(fire_test.write_verify_log(SimpleNamespace(run_dir=None), "x", r))

    def test_the_deploy_verify_is_labelled_interim(self):
        with self.assertRaises(TypeError):
            fire_test.stage_verify()          # args/comp/creds are required
        import inspect
        sig = inspect.signature(fire_test.stage_verify)
        self.assertIn("interim", sig.parameters["label"].default)


class CoTenancy(unittest.TestCase):

    def _comp(self, tmp, run_id="run-ours"):
        comp = Path(tmp) / "comp"
        comp.mkdir(exist_ok=True)
        (comp / ".deploy_state.json").write_text(json.dumps({"run_id": run_id}))
        return comp

    def test_another_range_on_the_same_node_is_reported(self):
        vms = [
            {"node": "proxmox", "status": "running", "tags": "tezcatlipoca;comp-comp;run-ours"},
            {"node": "proxmox", "status": "running", "tags": "tezcatlipoca;comp-comp;run-other"},
            {"node": "proxmox", "status": "running", "tags": "tezcatlipoca;comp-comp;run-other"},
            {"node": "proxmox", "status": "stopped", "tags": "tezcatlipoca;comp-comp;run-third"},
            {"node": "elsewhere", "status": "running", "tags": "tezcatlipoca;comp-comp;run-fourth"},
        ]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"TF_VAR_proxmox_node": "proxmox"}), \
             mock.patch("infra.client.proxmox_api", return_value={"data": vms}):
            others = staging.warn_co_tenancy(SimpleNamespace(), self._comp(tmp))
        self.assertEqual(others, {"run-other": 2})

    def test_an_idle_node_reports_none(self):
        vms = [{"node": "proxmox", "status": "running", "tags": "tezcatlipoca;comp-comp;run-ours"}]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"TF_VAR_proxmox_node": "proxmox"}), \
             mock.patch("infra.client.proxmox_api", return_value={"data": vms}):
            self.assertEqual(staging.warn_co_tenancy(SimpleNamespace(), self._comp(tmp)), {})

    def test_an_unreadable_api_does_not_break_the_deploy(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"TF_VAR_proxmox_node": "proxmox"}), \
             mock.patch("infra.client.proxmox_api", side_effect=RuntimeError("nope")):
            self.assertIsNone(staging.warn_co_tenancy(SimpleNamespace(), self._comp(tmp)))


class VerifyLogIsCollected(unittest.TestCase):

    def test_the_harness_want_list_carries_verify_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            targets = plan.plan_targets({"agents": {}, "paths": {"run_dir": tmp}}, comp_dir=tmp)
        harness = [t for t in targets if t["name"] == "harness"][0]
        names = [w["local_name"] for w in harness["want"]]
        self.assertIn("verify.log", names)
        self.assertIn("deploy.log", names)


if __name__ == "__main__":
    unittest.main()
