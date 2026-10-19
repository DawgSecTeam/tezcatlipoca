"""The engine mgmt-IP preflight."""

import io
import contextlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from infra import collisions as preflight_mgmt_ip  # noqa: E402


def _vm(vmid, node, name, status="running", template=0, tags="tezcatlipoca,comp-other"):
    return {"vmid": vmid, "node": node, "name": name, "status": status,
            "template": template, "tags": tags}


class _Cluster:
    def __init__(self, answers, agentless=()):
        self.answers = answers
        self.agentless = set(agentless)

    def __call__(self, method, path, **kwargs):
        vmid = int(path.split("/qemu/")[1].split("/")[0])
        if vmid in self.agentless:
            raise RuntimeError("500 QEMU guest agent is not running")
        return {"data": {"result": [
            {"ip-addresses": [{"ip-address": ip, "ip-address-type": "ipv4"}
                              for ip in self.answers.get(vmid, [])]}]}}


def _gate(vms, api, ip, engine_vmid=2400, node="proxmox", ours_tags=None, explicit=None,
          auto=False):
    # A previous test may have activated a per-node override on the process config.
    from estate import config
    config.reset()
    env = dict(os.environ)
    env.pop("TEZ_ENGINE_MGMT_IP", None)
    env.pop("TEZ_ENGINE_MGMT_IP_IS_DEFAULT", None)
    env.pop("TEZ_ENGINE_MGMT_IP_IS_AUTO_SELECTED", None)
    if explicit is not None:
        env["TEZ_ENGINE_MGMT_IP"] = explicit
    if auto:
        env["TEZ_ENGINE_MGMT_IP_IS_AUTO_SELECTED"] = "1"
    with patch.object(preflight_mgmt_ip, "proxmox_api", api), patch.dict(os.environ, env, clear=True):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            preflight_mgmt_ip.engine_mgmt_ip_gate(node, vms, engine_vmid, ip, ours_tags=ours_tags)
    return out.getvalue()


class ForeignGuestOnAnotherNode(unittest.TestCase):
    def test_a_guest_on_another_node_blocks_the_address(self):
        vms = [_vm(1100, "pve", "quotient-engine")]
        api = _Cluster({1100: ["10.0.0.250"]})
        with self.assertRaises(SystemExit) as ctx:
            _gate(vms, api, "10.0.0.250")
        message = str(ctx.exception)
        self.assertIn("10.0.0.250", message)
        self.assertIn("1100", message)
        self.assertIn("quotient-engine", message)
        self.assertIn("ANY node", message)

    def test_a_guest_on_this_node_still_blocks(self):
        vms = [_vm(1100, "proxmox", "quotient-engine")]
        with self.assertRaises(SystemExit):
            _gate(vms, _Cluster({1100: ["10.0.0.250"]}), "10.0.0.250")


class UnverifiableIsNotFree(unittest.TestCase):
    def test_the_default_ip_is_refused_when_any_guest_is_unverifiable(self):
        vms = [_vm(1100, "pve", "quotient-engine"), _vm(1200, "proxmox", "some-box")]
        api = _Cluster({1200: ["10.0.0.99"]}, agentless=[1100])
        with self.assertRaises(SystemExit) as ctx:
            _gate(vms, api, "10.0.0.250")
        message = str(ctx.exception)
        self.assertIn("cannot verify", message)
        self.assertIn("1100", message)
        self.assertIn("TEZ_ENGINE_MGMT_IP", message)


class EngineIpSelection(unittest.TestCase):
# The engine band's unattended selector mirrors red01's: lowest address nobody answers or
# claims, and NO selection at all while a running guest cannot be asked.

    def setUp(self):
        from estate import config
        config.reset()
        config.active().activate({"engine.mgmt_ip_band": ""})
        self.addCleanup(config.reset)

    def _config(self):
        from estate import config
        return config.active()

    def _select(self, vms, api, claimed=(), skip=(), ours=None):
        with patch.object(preflight_mgmt_ip, "proxmox_api", api):
            return preflight_mgmt_ip.select_engine_ip(vms, claimed, skip, ours)

    def test_skips_claimed_and_agent_reported_addresses(self):
        vms = [_vm(1100, "pve", "squatter")]
        api = _Cluster({1100: ["10.0.0.251", "10.0.0.252"]})
        ip, unchecked = self._select(vms, api, claimed={"10.0.0.253"})
        self.assertEqual(ip, "10.0.0.254")
        self.assertEqual(unchecked, [])

    def test_no_selection_when_a_running_guest_is_unverifiable(self):
        vms = [_vm(1100, "pve", "agentless")]
        ip, unchecked = self._select(vms, _Cluster({}, agentless=[1100]))
        self.assertIsNone(ip)
        self.assertEqual(len(unchecked), 1)
        self.assertIn("1100", unchecked[0])

    def test_two_concurrent_runs_get_distinct_addresses(self):
        api = _Cluster({})
        first, _ = self._select([], api)
        second, _ = self._select([], api, claimed={first})
        self.assertEqual((first, second), ("10.0.0.251", "10.0.0.252"))

    def test_default_and_red01_addresses_are_excluded_even_in_an_override(self):
        config = self._config()
        config.activate({"engine.mgmt_ip_band": "10.0.0.240-10.0.0.251"})
        ip, unchecked = self._select([], _Cluster({}))
        self.assertEqual(ip, "10.0.0.251")
        self.assertEqual(unchecked, [])


class AutoSelectedIsNotAnOperatorPin(unittest.TestCase):
# An auto-selected address was proven free when chosen, but it is not an operator pin:
# an incomplete gate scan must still refuse it.

    def test_auto_selected_ip_is_refused_when_a_guest_is_unverifiable(self):
        vms = [_vm(1100, "pve", "quotient-engine")]
        with self.assertRaises(SystemExit) as ctx:
            _gate(vms, _Cluster({}, agentless=[1100]), "10.0.0.251",
                  explicit="10.0.0.251", auto=True)
        self.assertIn("cannot verify", str(ctx.exception))

    def test_operator_pin_still_warns_and_proceeds(self):
        vms = [_vm(1100, "pve", "quotient-engine")]
        out = _gate(vms, _Cluster({}, agentless=[1100]), "10.0.0.251",
                    explicit="10.0.0.251")
        self.assertIn("UNVERIFIED", out)
        self.assertIn("set explicitly", out)


class SelfCollisionsAreNotForeign(unittest.TestCase):
    # Regression guard: a retry meets its own leftovers, which phase 1 recycles.

    def test_our_own_engine_vmid_is_skipped(self):
        vms = [_vm(2400, "proxmox", "engine")]
        out = _gate(vms, _Cluster({2400: ["10.0.0.250"]}), "10.0.0.250")
        self.assertIn("is free", out)

    def test_our_own_tagged_guests_are_skipped(self):
        ours = {"tezcatlipoca", "comp-same-type-2box"}
        vms = [_vm(1500, "proxmox", "web01", tags="tezcatlipoca,comp-same-type-2box")]
        out = _gate(vms, _Cluster({1500: ["10.0.0.250"]}), "10.0.0.250", ours_tags=ours)
        self.assertIn("is free", out)

    def test_a_foreign_guest_with_a_different_comp_tag_still_blocks(self):
        ours = {"tezcatlipoca", "comp-same-type-2box"}
        vms = [_vm(1100, "pve", "quotient-engine", tags="tezcatlipoca,comp-fixture")]
        with self.assertRaises(SystemExit):
            _gate(vms, _Cluster({1100: ["10.0.0.250"]}), "10.0.0.250", ours_tags=ours)


class JumpGateSharesTheScan(unittest.TestCase):
    def _plan(self, vms):
        from types import SimpleNamespace
        share = SimpleNamespace(node="proxmox", raw_vms=vms)
        return SimpleNamespace(
            placement={"satellites": [{"jump_mgmt_ip": "10.0.0.249", "jump_vmid": 900}]},
            engine_vmid=2400, our_tags={"tezcatlipoca", "comp-ours"}, shares=[share])

    def _run(self, vms, api):
        with patch.object(preflight_mgmt_ip, "proxmox_api", api):
            with contextlib.redirect_stdout(io.StringIO()):
                preflight_mgmt_ip.jump_mgmt_ip_gate(self._plan(vms))

    def test_a_foreign_guest_on_a_jump_ip_is_refused(self):
        vms = [_vm(1100, "proxmox", "quotient-engine", tags="tezcatlipoca,comp-fixture")]
        with self.assertRaises(SystemExit):
            self._run(vms, _Cluster({1100: ["10.0.0.249"]}))

    def test_our_own_tagged_guest_on_a_jump_ip_is_not_a_collision(self):
        vms = [_vm(1500, "proxmox", "web01", tags="tezcatlipoca,comp-ours")]
        self._run(vms, _Cluster({1500: ["10.0.0.249"]}))


if __name__ == "__main__":
    unittest.main()
