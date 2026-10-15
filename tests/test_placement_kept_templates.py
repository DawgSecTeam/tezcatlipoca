"""Kept templates are not vmid collisions.

`--keep-templates` / `destroy-competition.py --teams-only` leave this competition's engine
template and goldens on the node so the next deploy can adopt them (or rebuild them when the
node's `tezcatlipoca-m4 hash=` stamp no longer matches). The placement probe's quick veto
counted those vmids as collisions, so the node became ineligible for the very run the templates
were kept for and the deploy died at `no eligible node for team(s) …` before it could reuse
anything (live: run-0a59b188, 2026-10-14).
"""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from common.constants import ENGINE_TEMPLATE_VMID_OFFSET
from multinode import placement_planner

ENGINE = 1000
ET = ENGINE + ENGINE_TEMPLATE_VMID_OFFSET
GOLDENS = [1150, 1151, 1152, 1153, 1154, 1155]


def _vm(vmid, *, template=0, status="stopped", tags="tezcatlipoca", name=None):
    return {"vmid": vmid, "node": "proxmox", "template": template, "status": status,
            "tags": tags, "name": name or f"vm-{vmid}"}


def _api(vms):
    def fake(endpoint, token, method, path, params=None):
        if path.endswith("/status") and "/storage/" not in path:
            return {"data": {"memory": {"free": 8 * 1024 ** 3}}}
        if path == "/cluster/resources":
            return {"data": vms}
        if "/storage/" in path:
            return {"data": {"avail": 500 * 1024 ** 3}}
        if path.endswith("/network"):
            return {"data": []}
        raise AssertionError(path)
    return fake


class ProbeKeptTemplates(unittest.TestCase):

    def setUp(self):
        os.environ["TEST_PVE_TOKEN"] = "token"
        self.record = SimpleNamespace(name="hdd-150", node="proxmox", datastore="hdd",
                                      endpoint="https://10.0.0.150:8006",
                                      token_env="TEST_PVE_TOKEN")
        self.need = ["base-debian13-lite-fix"]

    def _probe(self, vms):
        vms = list(vms) + [_vm(955, template=1, tags="cloud-init;template",
                               name="base-debian13-lite-fix")]
        with mock.patch("infra.client.proxmox_api_for", _api(vms)):
            return placement_planner.probe_node(self.record, self.need, ENGINE, [120, 121])

    def test_our_kept_engine_template_and_goldens_are_not_collisions(self):
        probe = self._probe([_vm(ET, template=1, tags="comp-comp;engine-template;tezcatlipoca")]
                            + [_vm(g, template=1, tags="comp-comp;tezcatlipoca;tezcatlipoca-golden")
                               for g in GOLDENS])
        self.assertEqual(probe["collisions"], [])
        self.assertTrue(probe["ok"], probe["reasons"])

    def test_a_leftover_range_still_collides(self):
        probe = self._probe([_vm(ET, template=1, tags="comp-comp;tezcatlipoca"),
                             _vm(1000, status="running"),
                             _vm(1400, status="running")])
        self.assertEqual(probe["collisions"], [1000, 1400])
        self.assertFalse(probe["ok"])

    def test_a_foreign_template_in_our_slot_is_still_a_collision(self):
        probe = self._probe([_vm(ET, template=1, tags="other-team;template", name="someone-else")])
        self.assertEqual(probe["collisions"], [ET])
        self.assertFalse(probe["ok"])

    def test_a_running_vm_in_a_template_slot_is_still_a_collision(self):
        probe = self._probe([_vm(GOLDENS[0], template=0, status="running")])
        self.assertEqual(probe["collisions"], [GOLDENS[0]])


if __name__ == "__main__":
    unittest.main()
