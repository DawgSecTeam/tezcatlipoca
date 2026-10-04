"""The engine mgmt-IP preflight.

Live-found 2026-10-02, first attempt of the same-type-2box practice run: the gate
printed

    Preflight: engine mgmt IP 10.0.0.250 is free (4 running guest(s) unverifiable — agent down)

while a FOREIGN live `quotient-engine` was answering 10.0.0.250. The deploy then built
its engine-template VM on the same address and died at phase 2 with an opaque
`ssh ... exit status 255`. Two independent defects made that possible:

  * the scan skipped guests on other nodes, but 10.0.0.0/24 is ONE flat segment — the
    foreign engine was on .193 and the deploy was on .150, so the rows that mattered
    were filtered out even though `/cluster/resources` had already returned them;
  * a guest whose agent is down was counted and skipped, and the gate then concluded
    "free" from an absence of evidence.

Offline; the cluster resource list and the agent channel are faked.
"""

import io
import contextlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import config_ops  # noqa: E402
import preflight.mgmt_ip as preflight_mgmt_ip  # noqa: E402


def _vm(vmid, node, name, status="running", template=0, tags="tezcatlipoca,comp-other"):
    return {"vmid": vmid, "node": node, "name": name, "status": status,
            "template": template, "tags": tags}


class _Cluster:
    """Fake proxmox_api: returns agent interfaces per vmid, or raises when agentless."""

    def __init__(self, answers, agentless=()):
        self.answers = answers          # vmid -> [ip, ...]
        self.agentless = set(agentless)

    def __call__(self, method, path, **kwargs):
        vmid = int(path.split("/qemu/")[1].split("/")[0])
        if vmid in self.agentless:
            raise RuntimeError("500 QEMU guest agent is not running")
        return {"data": {"result": [
            {"ip-addresses": [{"ip-address": ip, "ip-address-type": "ipv4"}
                              for ip in self.answers.get(vmid, [])]}]}}


def _gate(vms, api, ip, engine_vmid=2400, node="proxmox", ours_tags=None, explicit=None):
    env = dict(os.environ)
    env.pop("TF_VAR_engine_mgmt_ip", None)
    if explicit is not None:
        env["TF_VAR_engine_mgmt_ip"] = explicit
    with patch.object(preflight_mgmt_ip, "proxmox_api", api), patch.dict(os.environ, env, clear=True):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            config_ops._engine_mgmt_ip_gate(node, vms, engine_vmid, ip, ours_tags=ours_tags)
    return out.getvalue()


class ForeignGuestOnAnotherNode(unittest.TestCase):
    """The exact live failure: the squatter was on .193 while the deploy was on .150."""

    def test_a_guest_on_another_node_blocks_the_address(self):
        vms = [_vm(1100, "pve", "quotient-engine")]
        api = _Cluster({1100: ["10.0.0.250"]})
        with self.assertRaises(SystemExit) as ctx:
            _gate(vms, api, "10.0.0.250")
        message = str(ctx.exception)
        self.assertIn("10.0.0.250", message)
        self.assertIn("1100", message)              # names the taker
        self.assertIn("quotient-engine", message)
        self.assertIn("ANY node", message)          # explains why a remote node counts

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
        self.assertIn("1100", message)                       # names what could not be asked
        self.assertIn("TF_VAR_engine_mgmt_ip", message)      # names the remedy

    def test_an_explicit_ip_warns_and_proceeds(self):
        vms = [_vm(1100, "pve", "quotient-engine")]
        api = _Cluster({}, agentless=[1100])
        out = _gate(vms, api, "10.0.0.247", explicit="10.0.0.247")
        self.assertIn("UNVERIFIED", out)
        self.assertIn("set explicitly", out)

    def test_a_clean_scan_says_free(self):
        vms = [_vm(1100, "pve", "other")]
        out = _gate(vms, _Cluster({1100: ["10.0.0.99"]}), "10.0.0.250")
        self.assertIn("is free", out)
        self.assertNotIn("UNVERIFIED", out)


class SelfCollisionsAreNotForeign(unittest.TestCase):
    """Regression guard: a retry meets its own leftovers, which phase 1 recycles."""

    def test_our_own_engine_vmid_is_skipped(self):
        vms = [_vm(2400, "proxmox", "engine")]
        out = _gate(vms, _Cluster({2400: ["10.0.0.250"]}), "10.0.0.250")
        self.assertIn("is free", out)

    def test_our_own_tagged_guests_are_skipped(self):
        ours = {"tezcatlipoca", "comp-same-type-2box-2026-09-29"}
        vms = [_vm(1500, "proxmox", "web01", tags="tezcatlipoca,comp-same-type-2box-2026-09-29")]
        out = _gate(vms, _Cluster({1500: ["10.0.0.250"]}), "10.0.0.250", ours_tags=ours)
        self.assertIn("is free", out)

    def test_a_foreign_guest_with_a_different_comp_tag_still_blocks(self):
        ours = {"tezcatlipoca", "comp-same-type-2box-2026-09-29"}
        vms = [_vm(1100, "pve", "quotient-engine", tags="tezcatlipoca,comp-cde-2026")]
        with self.assertRaises(SystemExit):
            _gate(vms, _Cluster({1100: ["10.0.0.250"]}), "10.0.0.250", ours_tags=ours)


class TemplatesAndStoppedGuestsAreIrrelevant(unittest.TestCase):
    def test_stopped_and_template_rows_are_ignored(self):
        vms = [_vm(1100, "pve", "t", status="stopped"),
               _vm(1200, "pve", "tpl", template=1)]
        out = _gate(vms, _Cluster({1100: ["10.0.0.250"], 1200: ["10.0.0.250"]}), "10.0.0.250")
        self.assertIn("is free", out)


if __name__ == "__main__":
    unittest.main()
