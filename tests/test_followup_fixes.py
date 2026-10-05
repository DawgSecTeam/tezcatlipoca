"""Regression tests for the 2026-10-04 follow-up fixes (live-found in the reset
matrix + scrim authoring, see docs/reports/reset-live-test-2026-10-03-report.md):

* F5 — the engine mgmt IP preflight must not treat the CODE-exported default as an
  operator-set address: an unverifiable DEFAULT is refused, an explicit one warned.
* F7 — ensure_packet_secrets compiles the packet's gitignored secret layer into a
  comp dir that arrived without it (stage_author/manual copy/fresh clone), so the
  machine list carries the packet-promised decoy accounts and the harness's pre-T0
  `verify --packet` cannot abort the run.
* F6 — a member that self-reports joined but has no machine account on the team's DC
  is a dead trust and must not read as a pass (verify's _member_trusted).
* F9 — reconcile_terraform_state imports rebuilt boxes by their deterministic vmid;
  a failed import warns instead of dying.
* F8 — env_summary names the estate (endpoint/node/datastore/tpl/mgmt) so a
  wrong-variant env copy shows in the banner.

Offline; no Proxmox, no terraform, no network."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import config_ops  # noqa: E402
import packet_ops  # noqa: E402
from utils import env_summary  # noqa: E402

import redeploy_rebuild_ops as redeploy  # noqa: E402  (owns reconcile_terraform_state)
from preflight import mgmt_ip as mgmt_ip_mod  # noqa: E402  (owns the engine mgmt-IP gate)


class EngineMgmtGateTests(unittest.TestCase):
    """F5: "explicitly set" must mean the OPERATOR set it."""

    GUEST = [{"vmid": 500, "name": "foreign-thing", "node": "n1", "status": "running",
              "tags": "", "template": 0}]

    def _gate(self):
        with patch.dict(os.environ, {"TF_VAR_proxmox_node": "n1"}, clear=False):
            config_ops._engine_mgmt_ip_gate("n1", self.GUEST, 1000, "10.0.0.250")

    def test_unverifiable_default_is_refused(self):
        with patch.dict(os.environ, {"TEZ_ENGINE_MGMT_IP_IS_DEFAULT": "1"}), \
             patch.object(mgmt_ip_mod, "proxmox_api", side_effect=RuntimeError("no agent")):
            with self.assertRaises(SystemExit):
                self._gate()

    def test_unverifiable_operator_set_ip_warns_and_proceeds(self):
        env = {k: v for k, v in os.environ.items() if k != "TEZ_ENGINE_MGMT_IP_IS_DEFAULT"}
        env["TF_VAR_engine_mgmt_ip"] = "10.0.0.250"  # operator-set, hermetic to ambient env
        with patch.dict(os.environ, env, clear=True), \
             patch.object(mgmt_ip_mod, "proxmox_api", side_effect=RuntimeError("no agent")):
            self._gate()  # must not raise

    def test_all_guests_checkable_reports_free(self):
        env = {k: v for k, v in os.environ.items() if k != "TEZ_ENGINE_MGMT_IP_IS_DEFAULT"}
        agent = {"data": {"result": [{"ip-addresses": [
            {"ip-address-type": "ipv4", "ip-address": "10.0.0.77"}]}]}}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(mgmt_ip_mod, "proxmox_api", return_value=agent):
            self._gate()  # reaches the "is free" print


class EnsurePacketSecretsTests(unittest.TestCase):
    """F7: the secret layer is compiled on demand, never silently absent."""

    COMPFILE = "name Test\npacket_source packets/cde-2026/packet.yaml\n"

    def _comp(self, tmp, compfile=COMPFILE):
        comp = Path(tmp) / "acomp"
        comp.mkdir()
        (comp / "Compfile").write_text(compfile)
        return comp

    def test_missing_secret_layer_is_compiled_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = self._comp(tmp)
            wrote = packet_ops.ensure_packet_secrets(comp)
            self.assertEqual(sorted(wrote),
                             ["box_baseline.json", "domain_accounts.json", "passwords.json"])
            baseline = json.loads((comp / "box_baseline.json").read_text())
            self.assertIn("web01", baseline)  # decoys for every managed box type
            for f in wrote:
                self.assertEqual(oct((comp / f).stat().st_mode & 0o777), "0o600")

    def test_second_run_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = self._comp(tmp)
            packet_ops.ensure_packet_secrets(comp)
            self.assertEqual(packet_ops.ensure_packet_secrets(comp), [])

    def test_missing_profile_is_a_loud_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = self._comp(tmp, "name T\npacket_source packets/nope/packet.yaml\n")
            with self.assertRaises(SystemExit):
                packet_ops.ensure_packet_secrets(comp)

    def test_no_packet_source_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = self._comp(tmp, "name T\n")
            self.assertEqual(packet_ops.ensure_packet_secrets(comp), [])

    def test_existing_secrets_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = self._comp(tmp)
            (comp / "box_baseline.json").write_text('{"keep": true}')
            packet_ops.ensure_packet_secrets(comp)
            self.assertEqual((comp / "box_baseline.json").read_text(), '{"keep": true}')

    def test_compile_profile_still_emits_the_same_secret_files(self):
        """The refactor must not change compile-packet's own output."""
        p = packet_ops.load_profile(_REPO / "packets/cde-2026/packet.yaml")
        files = packet_ops.packet_secret_files(p)
        self.assertEqual(sorted(files),
                         ["box_baseline.json", "domain_accounts.json", "passwords.json"])


class MemberTrustedTests(unittest.TestCase):
    """F6: the member's self-report is not the DC's truth."""

    def _job(self, role="member", team="team1"):
        return {"team_key": team, "role": role}

    def test_truth_table(self):
        from verifier import domains as vc
        pcs = {"team1": {"WINDOWS-AAA", "DC1"}}
        self.assertIs(vc._member_trusted(self._job(), {"HOST": "windows-aaa"}, pcs), True)
        self.assertIs(vc._member_trusted(self._job(), {"HOST": "windows-bbb"}, pcs), False)
        self.assertIs(vc._member_trusted(self._job(), {"HOST": ""}, pcs), None)
        self.assertIs(vc._member_trusted(self._job(), {}, {}), None)  # no DC listing
        self.assertIs(vc._member_trusted(self._job(role="dc"), {"HOST": "DC1"}, pcs), None)


class ReconcileStateTests(unittest.TestCase):
    """F9: import rebuilt boxes by deterministic vmid; warn, never die."""

    def _target(self, vm_name, vmid, slot=0):
        return {"vm_name": vm_name, "vmid": vmid, "slot": slot}

    def test_imports_slot0_and_satellite_addresses(self):
        calls = []
        env = {"TF_VAR_proxmox_node": "pve"}
        with patch.dict(os.environ, env):
            with patch.object(redeploy, "run_terraform",
                              side_effect=lambda *a, **k: calls.append((a[0], k))):
                redeploy.reconcile_terraform_state(
                    [self._target("team1-db01", 1503), self._target("130-web01", 1504, slot=2)],
                    {}, Path("/tmp/comp"), {}, [])
        self.assertEqual(calls[0][0], ["state", "rm",
                          'proxmox_virtual_environment_vm.team_box["team1-db01"]'])
        self.assertEqual(calls[1][0], ["import",
                          'proxmox_virtual_environment_vm.team_box["team1-db01"]', "pve/1503"])
        self.assertEqual(calls[2][0], ["state", "rm",
                          'proxmox_virtual_environment_vm.team_box_sat2["130-web01"]'])
        self.assertEqual(calls[3][0], ["import",
                          'proxmox_virtual_environment_vm.team_box_sat2["130-web01"]', "pve/1504"])
        self.assertIn("cwd", calls[0][1])

    def test_failed_import_warns_and_continues(self):
        with patch.dict(os.environ, {"TF_VAR_proxmox_node": "pve"}), \
             patch.object(redeploy, "run_terraform",
                          side_effect=RuntimeError("provider cannot import")):
            redeploy.reconcile_terraform_state([self._target("team1-db01", 1503)],
                                               {}, Path("/tmp/comp"), {}, [])  # no raise


class EnvSummaryTests(unittest.TestCase):
    """F8: the banner line names the estate."""

    def test_summary_carries_all_five_facts(self):
        env = {"TF_VAR_proxmox_endpoint": "https://10.0.0.150:8006",
               "TF_VAR_proxmox_node": "proxmox", "TF_VAR_datastore": "wkshp-pool",
               "TF_VAR_template_vm_id": "955", "TF_VAR_engine_mgmt_ip": "10.0.0.252"}
        with patch.dict(os.environ, env, clear=True):
            s = env_summary()
        for fact in ("10.0.0.150", "proxmox", "wkshp-pool", "955", "10.0.0.252"):
            self.assertIn(fact, s)

    def test_unset_mgmt_ip_reads_as_default(self):
        env = {"TF_VAR_proxmox_endpoint": "https://x", "TF_VAR_proxmox_node": "n",
               "TF_VAR_datastore": "d", "TF_VAR_template_vm_id": "1"}
        with patch.dict(os.environ, env, clear=True):
            self.assertIn("engine-mgmt=default", env_summary())


if __name__ == "__main__":
    unittest.main()
