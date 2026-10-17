"""Regression tests for the follow-up fixes (live-found in the reset
matrix + scrim authoring):

* The engine mgmt IP preflight must not treat the CODE-exported default as an
  operator-set address: an unverifiable DEFAULT is refused, an explicit one warned.
* ensure_packet_secrets compiles the packet's gitignored secret layer into a
  comp dir that arrived without it (stage_author/manual copy/fresh clone), so the
  machine list carries the packet-promised decoy accounts and the harness's pre-T0
  `verify --packet` cannot abort the run.
* A member that self-reports joined but has no machine account on the team's DC
  is a dead trust and must not read as a pass (verify's _member_trusted).
* reconcile_estate_state imports rebuilt boxes by their deterministic vmid;
  a failed import warns instead of dying.
* env_summary names the estate (endpoint/node/datastore/tpl/mgmt) so a
  wrong-variant env copy shows in the banner.

Offline; no Proxmox, no network."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from config import packet_ops  # noqa: E402
from common.utils import env_summary  # noqa: E402

from estate import state as estate_state  # noqa: E402
from redeploy import redeploy_rebuild_ops as redeploy  # noqa: E402  (owns reconcile_estate_state)
from redeploy.redeploy_context import RedeployContext  # noqa: E402


def _rctx(comp_dir, teams, boxes, ctx=None, node="pve"):
    return RedeployContext(comp_dir=comp_dir, name="x", comp_name="x", node=node,
                           teams=teams, boxes=boxes, difficulty=None, state={},
                           state_path=None, ctx={} if ctx is None else ctx)
from infra import collisions as mgmt_ip_mod  # noqa: E402  (owns the engine mgmt-IP gate)


class EngineMgmtGateTests(unittest.TestCase):
# F5: "explicitly set" must mean the OPERATOR set it.

    GUEST = [{"vmid": 500, "name": "foreign-thing", "node": "n1", "status": "running",
              "tags": "", "template": 0}]

    def _gate(self):
        with patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "n1"}, clear=False):
            mgmt_ip_mod.engine_mgmt_ip_gate("n1", self.GUEST, 1000, "10.0.0.250")

    def test_unverifiable_default_is_refused(self):
        with patch.dict(os.environ, {"TEZ_ENGINE_MGMT_IP_IS_DEFAULT": "1"}), \
             patch.object(mgmt_ip_mod, "proxmox_api", side_effect=RuntimeError("no agent")):
            with self.assertRaises(SystemExit):
                self._gate()

    def test_unverifiable_operator_set_ip_warns_and_proceeds(self):
        env = {k: v for k, v in os.environ.items() if k != "TEZ_ENGINE_MGMT_IP_IS_DEFAULT"}
        env["TEZ_ENGINE_MGMT_IP"] = "10.0.0.250"  # operator-set, hermetic to ambient env
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
    # packet colocated with the comp dir: packet_source "packet.yaml" resolves
    # comp-dir-relative, so the fixture needs no comps checkout at all.
    COMPFILE = "name Test\npacket_source packet.yaml\n"
    PACKET = """
event:
  comp_id: packet-secrets-fixture
  name: Packet Secrets Fixture
  scenario: fixture
  difficulty: 3
credentials:
  box_username: ubuntu
  box_password: fixture-pass-1
  credlists:
    linux:
      svc01: service-pass-1
  out_of_scope: [scorebot]
  domain_accounts:
    - {username: Blue, password: Domain-Pass-1}
boxes:
  - {name: web01, last_octet: 2, template: base-ubuntu24.04-fix, fidelity: exact}
  - {name: fw01, last_octet: 1, template: base-pfsense, unmanaged: true,
     fidelity: substituted}
"""

    def _comp(self, tmp, compfile=COMPFILE):
        comp = Path(tmp) / "acomp"
        comp.mkdir()
        (comp / "Compfile").write_text(compfile)
        (comp / "packet.yaml").write_text(self.PACKET)
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
            comp = self._comp(tmp, "name T\npacket_source nope/packet.yaml\n")
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


class MemberTrustedTests(unittest.TestCase):
# F6: the member's self-report is not the DC's truth.

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
# F9: import rebuilt boxes by deterministic vmid; warn, never die.

    def _target(self, vm_name, vmid, slot=0):
        return {"vm_name": vm_name, "vmid": vmid, "slot": slot}

    def test_records_every_rebuilt_box_against_its_new_vmid(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            with patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "pve"}):
                redeploy.reconcile_estate_state(
                    [self._target("team1-db01", 1503), self._target("130-web01", 1504, slot=2)],
                    _rctx(comp_dir, {}, []))
            recorded = {r["name"]: r["vmid"]
                        for r in estate_state.EstateState.load(comp_dir).resources("team_box")}
        self.assertEqual(recorded, {"team1-db01": 1503, "130-web01": 1504})

    def test_reconciling_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            with patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "pve"}):
                for _ in range(2):
                    redeploy.reconcile_estate_state([self._target("team1-db01", 1503)],
                                                    _rctx(comp_dir, {}, []))
            rows = estate_state.EstateState.load(comp_dir).resources("team_box")
        self.assertEqual([r["vmid"] for r in rows], [1503])


class EnvSummaryTests(unittest.TestCase):
    def test_summary_carries_all_five_facts(self):
        env = {"TEZ_PROXMOX_ENDPOINT": "https://203.0.113.10:8006",
               "TEZ_PROXMOX_NODE": "proxmox", "TEZ_PROXMOX_DATASTORE": "pve-pool",
               "TEZ_PROXMOX_TEMPLATE_VM_ID": "5001", "TEZ_ENGINE_MGMT_IP": "10.0.0.248"}
        with patch.dict(os.environ, env, clear=True):
            s = env_summary()
        for fact in ("203.0.113.10", "proxmox", "pve-pool", "5001", "10.0.0.248"):
            self.assertIn(fact, s)

    def test_unset_mgmt_ip_reads_as_default(self):
        env = {"TEZ_PROXMOX_ENDPOINT": "https://x", "TEZ_PROXMOX_NODE": "n",
               "TEZ_PROXMOX_DATASTORE": "d", "TEZ_PROXMOX_TEMPLATE_VM_ID": "1"}
        with patch.dict(os.environ, env, clear=True):
            self.assertIn("engine-mgmt=default", env_summary())

    def test_summary_reads_the_resolved_target_not_the_ambient_env(self):
        """Placement and --engine-mgmt-ip move the run by activating the config, not the
        environment. The banner must name what the run DIALS: failed once when a canary
        deployed --engine-mgmt-ip 10.0.0.246 while every line printed 10.0.0.248."""
        from estate import config
        env = {"TEZ_PROXMOX_ENDPOINT": "https://203.0.113.10:8006",
               "TEZ_PROXMOX_NODE": "proxmox", "TEZ_PROXMOX_DATASTORE": "hdd",
               "TEZ_PROXMOX_TEMPLATE_VM_ID": "5001", "TEZ_ENGINE_MGMT_IP": "10.0.0.248"}
        with patch.dict(os.environ, env, clear=True):
            config.reset()
            config.active().activate({"engine.mgmt_ip": "10.0.0.246",
                                      "proxmox.endpoint": "https://203.0.113.20:8006"})
            s = env_summary()
        self.assertIn("engine-mgmt=10.0.0.246", s)
        self.assertIn("endpoint=https://203.0.113.20:8006", s)


if __name__ == "__main__":
    unittest.main()
