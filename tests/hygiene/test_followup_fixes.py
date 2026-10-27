"""Follow-up fix regressions: mgmt-IP gate, packet secrets, member trust, reconcile, env banner."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from config import packet_ops  # noqa: E402
from common.utils import env_summary  # noqa: E402

from estate import state as estate_state  # noqa: E402
from redeploy import redeploy_rebuild_ops as redeploy  # noqa: E402
from redeploy.redeploy_context import RedeployContext  # noqa: E402


def _rctx(comp_dir, teams, boxes, ctx=None, node="pve"):
    return RedeployContext(comp_dir=comp_dir, name="x", comp_name="x", node=node,
                           teams=teams, boxes=boxes, difficulty=None, state={},
                           state_path=None, ctx={} if ctx is None else ctx)
from infra import collisions as mgmt_ip_mod  # noqa: E402


class EngineMgmtGateTests(unittest.TestCase):
    # "explicitly set" must mean the OPERATOR set it.

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
        env["TEZ_ENGINE_MGMT_IP"] = "10.0.0.250"
        with patch.dict(os.environ, env, clear=True), \
             patch.object(mgmt_ip_mod, "proxmox_api", side_effect=RuntimeError("no agent")):
            self._gate()



class EnsurePacketSecretsTests(unittest.TestCase):
    # packet_source "packet.yaml" resolves comp-dir-relative: no comps checkout needed.
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


    def test_missing_profile_is_a_loud_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = self._comp(tmp, "name T\npacket_source nope/packet.yaml\n")
            with self.assertRaises(SystemExit):
                packet_ops.ensure_packet_secrets(comp)


    def test_existing_secrets_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = self._comp(tmp)
            (comp / "box_baseline.json").write_text('{"keep": true}')
            packet_ops.ensure_packet_secrets(comp)
            self.assertEqual((comp / "box_baseline.json").read_text(), '{"keep": true}')


class MemberTrustedTests(unittest.TestCase):
    # the member's self-report is not the DC's truth.

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


class EnvSummaryTests(unittest.TestCase):


    def test_summary_reads_the_resolved_target_not_the_ambient_env(self):
        """The banner must name the activated config the run dials, not the ambient env."""
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
