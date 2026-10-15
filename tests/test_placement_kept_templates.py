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
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from common.constants import ENGINE_TEMPLATE_VMID_OFFSET
from deploy_lib import golden_plan
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


class GoldenRebuildAdoption(unittest.TestCase):
    """A drifted golden kept from an EARLIER run must still be rebuildable.

    run-27b0d9e4 (2026-10-14) died in phase 4: golden-win02's node stamp no longer matched, the
    gate went to rebuild it, and the ownership proof refused — the kept template carries the
    PREVIOUS run's tag while the reclaim tag falls back to this run's id. Adoption (the path a
    REUSED engine template already takes) makes the destroy legitimate.
    """

    def test_adoption_requires_our_comp_tag_and_the_golden_marker(self):
        def api(cfg):
            return lambda *a, **k: {"data": cfg}
        own = {"template": 1, "tags": "comp-c1;tezcatlipoca;tezcatlipoca-golden;run-old"}
        with mock.patch("infra.client.proxmox_api", api(own)):
            self.assertTrue(golden_plan._is_own_golden_template("pve", 1151, "c1"))
        foreign = {"template": 1, "tags": "comp-other;tezcatlipoca;tezcatlipoca-golden"}
        with mock.patch("infra.client.proxmox_api", api(foreign)):
            self.assertFalse(golden_plan._is_own_golden_template("pve", 1151, "c1"))
        running = {"template": 0, "tags": own["tags"]}
        with mock.patch("infra.client.proxmox_api", api(running)):
            self.assertFalse(golden_plan._is_own_golden_template("pve", 1151, "c1"))

    def test_a_drifted_golden_is_adopted_before_it_is_destroyed(self):
        calls = []
        with mock.patch.object(golden_plan, "_is_template", lambda n, v: True), \
             mock.patch.object(golden_plan, "template_stamp_current", lambda n, v, h: False), \
             mock.patch.object(golden_plan, "frozen_gate", lambda *a, **k: True), \
             mock.patch.object(golden_plan, "_is_own_golden_template", lambda n, v, c: True), \
             mock.patch.object(golden_plan, "retag_ownership",
                               lambda n, v, o: calls.append(("retag", v, sorted(o)))), \
             mock.patch.object(golden_plan, "destroy_vm_if_exists",
                               lambda n, v, expect_tags=None: calls.append(("destroy", v))):
            golden_plan.golden_rebuild_gate(
                Path("comp"), "pve", 0, [{"name": "web01", "template": "t"}], 1000, {},
                {"web01": "hash"}, {"web01": {"config": {}, "code": {}}}, "c1", run_tag="run-new")
        self.assertEqual([c[0] for c in calls], ["retag", "destroy"])
        self.assertIn("run-new", calls[0][2])

    def test_a_foreign_template_is_not_adopted(self):
        calls = []
        with mock.patch.object(golden_plan, "_is_template", lambda n, v: True), \
             mock.patch.object(golden_plan, "template_stamp_current", lambda n, v, h: False), \
             mock.patch.object(golden_plan, "frozen_gate", lambda *a, **k: True), \
             mock.patch.object(golden_plan, "_is_own_golden_template", lambda n, v, c: False), \
             mock.patch.object(golden_plan, "retag_ownership",
                               lambda n, v, o: calls.append("retag")), \
             mock.patch.object(golden_plan, "destroy_vm_if_exists",
                               lambda n, v, expect_tags=None: calls.append("destroy")):
            golden_plan.golden_rebuild_gate(
                Path("comp"), "pve", 0, [{"name": "web01", "template": "t"}], 1000, {},
                {"web01": "hash"}, {"web01": {"config": {}, "code": {}}}, "c1", run_tag="run-new")
        self.assertEqual(calls, ["destroy"])       # destroy still refuses it, loudly


if __name__ == "__main__":
    unittest.main()
