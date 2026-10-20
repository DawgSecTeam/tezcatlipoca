"""red01's preflight gates: a reserved vmid/IP is refused when anything else holds it.

red01 is a tezcatlipoca-owned VM, so the gate classifies an occupant by this
run's tag set instead of a second checkout's marker.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from common.constants import comp_tag  # noqa: E402
from infra import red01  # noqa: E402
from infra.plan import NodeShare, PreflightPlan, single_node_plan  # noqa: E402

BOXES = [{"name": "web01", "template": "t"}]
TEAMS = {"team1": {"identifier": "130"}}


def _comp(d):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    (d / "Compfile").write_text(f"name {d.name}\n")
    return d


def _vm(vmid, status="running"):
    return {"vmid": vmid, "name": f"vm{vmid}", "node": "pve", "status": status}


def _plan(comp, vms=(), resuming=False, red01_vmid=1470, red01_ip="10.0.0.198",
          red01_ip_explicit=False):
    share = NodeShare(name="", node="pve", multi=False, slot=0, is_engine=True,
                      teams=TEAMS, num_teams=1, datastore="ds",
                      fetch_vms=lambda: list(vms))
    share.raw_vms = list(vms)
    return PreflightPlan(Path(comp), BOXES, 1300, [share], our_run_tag="run-test",
                         red01_vmid=red01_vmid, red01_ip=red01_ip,
                         red01_ip_explicit=red01_ip_explicit, resuming=resuming)


class VmidGate(unittest.TestCase):

    def test_our_full_tag_set_on_resume_is_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            comp = _comp(td)
            tags = {"tezcatlipoca", comp_tag(comp.name), "run-test"}
            with patch.object(red01, "_vm_tags", return_value=tags):
                red01.red01_vmid_gate(_plan(comp, vms=[_vm(1470)], resuming=True))

    def test_another_run_of_this_competition_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as td:
            comp = _comp(td)
            tags = {"tezcatlipoca", comp_tag(comp.name), "run-other"}
            with patch.object(red01, "_vm_tags", return_value=tags):
                with self.assertRaises(SystemExit) as cm:
                    red01.red01_vmid_gate(_plan(comp, vms=[_vm(1470)]))
            self.assertIn("another run of this competition", str(cm.exception))

    def test_foreign_occupant_refused_and_named(self):
        with tempfile.TemporaryDirectory() as td:
            comp = _comp(td)
            with patch.object(red01, "_vm_tags",
                              return_value={"tezcatlipoca", "comp-other-range"}):
                with self.assertRaises(SystemExit) as cm:
                    red01.red01_vmid_gate(_plan(comp, vms=[_vm(1470)]))
            self.assertIn("already occupied", str(cm.exception))
            self.assertIn("comp-other-range", str(cm.exception))

    def test_unreadable_tags_are_refused(self):
        with tempfile.TemporaryDirectory() as td:
            comp = _comp(td)
            with patch.object(red01, "_vm_tags", return_value=None):
                with self.assertRaises(SystemExit) as cm:
                    red01.red01_vmid_gate(_plan(comp, vms=[_vm(1470)]))
            self.assertIn("could not be classified", str(cm.exception))


class IpGate(unittest.TestCase):
    def _scan(self, comp, addrs_by_vmid, resuming=False, **kw):
        vms = [_vm(v) for v in addrs_by_vmid]
        with patch("infra.red01.guest_ipv4s",
                   side_effect=lambda vm: addrs_by_vmid[vm["vmid"]]):
            red01.red01_ip_gate(_plan(comp, vms=vms, resuming=resuming, **kw))


    def test_answering_guest_refused(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                self._scan(_comp(td), {100: ["10.0.0.50"], 200: ["10.0.0.198"]})
            self.assertIn("10.0.0.198", str(cm.exception))
            self.assertIn("already answered", str(cm.exception))


    def test_unverifiable_default_ip_refused(self):
        with tempfile.TemporaryDirectory() as td:
            with patch("infra.red01.guest_ipv4s", return_value=None):
                with self.assertRaises(SystemExit) as cm:
                    red01.red01_ip_gate(_plan(_comp(td), vms=[_vm(100)]))
            self.assertIn("no working agent", str(cm.exception))



class GateWiring(unittest.TestCase):
    def test_gate_is_inert_without_a_resolved_slot(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(red01, "red01_vmid_gate") as vmid_gate:
                red01.red01_gate(_plan(_comp(td), vms=[_vm(1470)], red01_vmid=0))
            vmid_gate.assert_not_called()

    def test_gate_runs_when_the_slot_is_resolved(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(red01, "red01_vmid_gate") as vmid_gate, \
                    patch.object(red01, "red01_ip_gate") as ip_gate:
                red01.red01_gate(_plan(_comp(td)))
            vmid_gate.assert_called_once()
            ip_gate.assert_called_once()


class PlanCarriesTheReservation(unittest.TestCase):
    def test_single_node_plan_resolves_vmid_and_ip(self):
        with tempfile.TemporaryDirectory() as td:
            comp = _comp(td)
            (comp / "Compfile").write_text("name c\nred01_vmid 1777\nred01_ip 10.0.0.241\n")
            plan = single_node_plan(comp, BOXES, 1, TEAMS, 1300, True, "", "run-test")
        self.assertEqual(plan.red01_vmid, 1777)
        self.assertEqual(plan.red01_ip, "10.0.0.241")
        self.assertTrue(plan.red01_ip_explicit)


if __name__ == "__main__":
    unittest.main()
