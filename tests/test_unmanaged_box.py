"""Unmanaged box type (pfSense/appliance): skipped by nakon plant + golden set, kept positional.
Offline; pinned-config mode so no vulndb/nakon call."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import utils
import nakon_ops
from golden_ops import golden_targets, golden_vmid_for

BOXES = [
    {"name": "fw01", "last_octet": 1, "template": "pfsense-fix", "unmanaged": True},
    {"name": "dc01", "last_octet": 2, "template": "base-windows-server"},
    {"name": "web01", "last_octet": 4, "template": "base-ubuntu24.04-fix"},
    {"name": "app01", "last_octet": 5, "template": "base-fedora44"},
]
TEAMS = {"team1": {"identifier": "120", "password": "x"}}


class UnmanagedBox(unittest.TestCase):
    def test_predicate(self):
        self.assertTrue(utils.is_unmanaged(BOXES[0]))
        self.assertFalse(utils.is_unmanaged(BOXES[1]))
        self.assertFalse(utils.is_unmanaged({"name": "z", "template": "t"}))

    def test_in_path_predicate_implies_unmanaged_gates(self):
        # in_path is a SUBSET of unmanaged: every unmanaged skip must also skip it, so
        # the phase-5 network plumbing is the ONLY thing the flag adds.
        fw = {**BOXES[0], "in_path": True}
        self.assertTrue(utils.is_in_path_fw(fw))
        self.assertTrue(utils.is_unmanaged(fw))
        self.assertFalse(utils.is_in_path_fw(BOXES[0]))  # unmanaged alone: no plumbing
        self.assertFalse(utils.is_in_path_fw({**BOXES[1], "in_path": True}))  # not unmanaged

    def test_golden_targets_skip_unmanaged_positional(self):
        gt = golden_targets(1080, TEAMS, BOXES)
        names = [t["box"]["name"] for t in gt]
        self.assertEqual(names, ["dc01", "web01", "app01"])  # fw01 excluded
        # box_idx stays positional (matches full-list index), so vmids don't shift
        by = {t["box"]["name"]: t for t in gt}
        self.assertEqual(by["dc01"]["box_idx"], 1)
        self.assertEqual(by["dc01"]["vmid"], golden_vmid_for(1080, 1))
        self.assertEqual(by["app01"]["box_idx"], 3)

    def test_nakon_config_omits_unmanaged_machine(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            # pinned mode → no vulndb/nakon call; give managed boxes empty pins
            (d / "box_services.json").write_text(json.dumps({"dc01": [], "web01": [], "app01": []}))
            (d / "box_vulns.json").write_text(json.dumps({"dc01": [], "web01": [], "app01": []}))
            path = nakon_ops.generate_nakon_config(TEAMS, BOXES, 1, d, "pw", box_username="medic")
            cfg = json.loads(Path(path).read_text())
            names = [m["name"] for m in cfg["machines"]]
            self.assertNotIn("fw01-team120", names)
            self.assertEqual(set(names), {"dc01-team120", "web01-team120", "app01-team120"})
            # fw01 must not be written into the scored-service map
            self.assertNotIn("fw01", json.loads((d / "box_services.json").read_text()))


if __name__ == "__main__":
    unittest.main()
