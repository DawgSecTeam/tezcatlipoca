"""The management bridge knob: nodes.json record -> TF_VAR_mgmt_bridge -> vmbr0, and the
preflight refusal when a node's network listing cannot show the configured bridge."""

import contextlib
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

from common.constants import DEFAULT_MGMT_BRIDGE  # noqa: E402
from deploy_lib import tfinputs  # noqa: E402
from deploy_lib.context import DeployContext  # noqa: E402
from infra.collisions import check_mgmt_bridge  # noqa: E402
from infra.gates import run_preflight  # noqa: E402
from infra.plan import NodeShare, PreflightPlan  # noqa: E402
from multinode import nodes_config  # noqa: E402


def _rec(**kw):
    base = dict(name="n1", endpoint="https://n1:8006", node="PVE1", datastore="ds",
                token_env="TOK_N1")
    base.update(kw)
    return nodes_config.NodeRecord(**base)


class ResolutionTest(unittest.TestCase):
    def test_default_when_nothing_is_set(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(tfinputs.mgmt_bridge_from_env(), DEFAULT_MGMT_BRIDGE)
            self.assertEqual(os.environ["TF_VAR_mgmt_bridge"], "vmbr0")

    def test_env_leg_is_kept(self):
        with patch.dict(os.environ, {"TF_VAR_mgmt_bridge": "vmbr100"}, clear=True):
            self.assertEqual(tfinputs.mgmt_bridge_from_env(), "vmbr100")

    def test_record_leg_wins_over_the_env_and_is_restored(self):
        with patch.dict(os.environ,
                        {"TOK_N1": "tok", "TF_VAR_mgmt_bridge": "vmbr0"}, clear=True):
            restore = nodes_config.apply_node_env(_rec(mgmt_bridge="vmbr100"))
            try:
                self.assertEqual(tfinputs.mgmt_bridge_from_env(), "vmbr100")
            finally:
                nodes_config.restore_node_env(restore)
            self.assertEqual(os.environ["TF_VAR_mgmt_bridge"], "vmbr0")

    def test_empty_record_field_leaves_the_env_alone(self):
        with patch.dict(os.environ,
                        {"TOK_N1": "tok", "TF_VAR_mgmt_bridge": "vmbr100"}, clear=True):
            restore = nodes_config.apply_node_env(_rec())
            try:
                self.assertEqual(tfinputs.mgmt_bridge_from_env(), "vmbr100")
            finally:
                nodes_config.restore_node_env(restore)


class TfvarsCarryBridge(unittest.TestCase):
    def test_resolved_bridge_reaches_terraform(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            tf_dir = comp_dir / "terraform"
            tf_dir.mkdir()
            ctx = DeployContext(comp_name="c", boxes=[{"name": "web01", "template": "t"}],
                                teams={}, number_of_teams=0, engine_vmid=1000)
            captured = {}
            env = {"TF_VAR_ssh_private_key_path": "/tmp/key",
                   "TF_VAR_proxmox_node": "pve",
                   "TF_VAR_proxmox_endpoint": "https://pve:8006/",
                   "TF_VAR_mgmt_bridge": "vmbr100"}
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(tfinputs, "ensure_terraform_workdir", return_value=tf_dir), \
                    patch.object(tfinputs, "write_text_atomic",
                                 side_effect=lambda p, t: captured.__setitem__(str(p), t)):
                tfinputs.build_terraform_inputs(ctx, comp_dir)
                # The env export is what terraform reads natively; the tfvars copy
                # survives a resume's apply #2.
                self.assertEqual(os.environ["TF_VAR_mgmt_bridge"], "vmbr100")
            tfvars = json.loads(captured[str(tf_dir / "terraform.tfvars.json")])
        self.assertEqual(tfvars["mgmt_bridge"], "vmbr100")


def _share(node="PVE1", is_engine=True, needs_jump=False, bridge=""):
    return NodeShare(name="", node=node, multi=False, slot=0, is_engine=is_engine,
                     teams={}, num_teams=0, datastore="ds", fetch_vms=lambda: [],
                     check_templates=False, check_collisions=False, check_headroom=False,
                     needs_jump_template=needs_jump, mgmt_bridge=bridge)


class BridgeGateTest(unittest.TestCase):
    def _run(self, listing, bridge="vmbr100", share=None):
        plan = PreflightPlan(Path("competitions/c1"), [], 1000, [share or _share()])
        with patch.dict(os.environ, {"TF_VAR_mgmt_bridge": bridge}, clear=True), \
                patch("infra.collisions.proxmox_api", return_value={"data": listing}), \
                patch("infra.gates.remote_access_gate"), \
                patch("infra.gates.catalog_gate"), \
                patch("infra.red01.red01_gate"):
            run_preflight(plan)

    def test_missing_bridge_is_refused_naming_the_bridge_and_node(self):
        with self.assertRaises(SystemExit) as cm:
            self._run([{"iface": "vmbr0", "type": "bridge"},
                       {"iface": "eth0", "type": "eth"}])
        self.assertIn("vmbr100", str(cm.exception))
        self.assertIn("PVE1", str(cm.exception))

    def test_present_bridge_passes(self):
        self._run([{"iface": "eth0", "type": "eth"},
                   {"iface": "vmbr100", "type": "bridge"}])

    def test_a_satellites_jump_nic_is_checked_on_its_own_node(self):
        share = _share(node="PVE2", is_engine=False, needs_jump=True)
        with self.assertRaises(SystemExit) as cm:
            self._run([{"iface": "vmbr0", "type": "bridge"}], share=share)
        self.assertIn("PVE2", str(cm.exception))

    def test_a_share_that_creates_nothing_is_not_checked(self):
        share = _share(node="PVE3", is_engine=False, needs_jump=False)
        self._run([{"iface": "vmbr0", "type": "bridge"}], share=share)

    def test_a_satellites_own_bridge_wins_over_the_env(self):
        # A satellite record's mgmt_bridge is per-node; only the ENGINE record reaches
        # TF_VAR_mgmt_bridge, so the share's own value must drive its own check.
        share = _share(node="PVE2", is_engine=False, needs_jump=True, bridge="vmbr200")
        with self.assertRaises(SystemExit) as cm:
            self._run([{"iface": "vmbr100", "type": "bridge"}], bridge="vmbr100", share=share)
        self.assertIn("vmbr200", str(cm.exception))


class BridgeVisibilityCaveatTest(unittest.TestCase):
    # PVE filters bridge entries for a token without SDN.Audit/SDN.Use, so a listing
    # with no bridge at all is unverifiable — a warning, never a refusal (which would
    # be a false negative on every estate whose token is scoped that way).
    def test_no_visible_bridge_warns_and_proceeds(self):
        for listing in ([], [{"iface": "eth0", "type": "eth"}]):
            out = io.StringIO()
            with patch("infra.collisions.proxmox_api",
                       return_value={"data": listing}), \
                    contextlib.redirect_stdout(out):
                check_mgmt_bridge("PVE1", "vmbr100", label="[n1]")
            self.assertIn("WARNING", out.getvalue())
            self.assertIn("vmbr100", out.getvalue())


if __name__ == "__main__":
    unittest.main()
