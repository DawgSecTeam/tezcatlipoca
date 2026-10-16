"""red01's preflight gates — the 2026-10-12 vmid-999 kill was this exact gap.

red01 is bad-auto's VM (no tezcatlipoca ownership tags), so it cannot ride
the clash gate's slot math. These gates refuse a deploy whose reserved red01
vmid/IP is occupied by anything live — a second session defaulting onto a
shared slot is a REFUSAL naming the owner, not a warning."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from infra import red01  # noqa: E402
from infra.plan import NodeShare, PreflightPlan, single_node_plan  # noqa: E402

BOXES = [{"name": "web01", "template": "t"}]
TEAMS = {"team1": {"identifier": "130"}}


def _comp(d, extra=""):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    (d / "Compfile").write_text(f"name {d.name}\n{extra}")
    return d


def _plan(comp, vms=(), resuming=False, red01_vmid=1470, red01_ip="10.0.0.198",
          red01_ip_explicit=False):
    share = NodeShare(name="", node="pve", multi=False, slot=0, is_engine=True,
                      teams=TEAMS, num_teams=1, datastore="ds",
                      fetch_vms=lambda: list(vms))
    share.raw_vms = list(vms)
    return PreflightPlan(Path(comp), BOXES, 1300, [share], red01_vmid=red01_vmid,
                         red01_ip=red01_ip, red01_ip_explicit=red01_ip_explicit,
                         resuming=resuming)


def _vm(vmid, status="running"):
    return {"vmid": vmid, "name": f"vm{vmid}", "node": "pve", "status": status}


class VmidGate(unittest.TestCase):
    def _marker_desc(self, comp):
        return {"data": {"description": f"bad-auto red01 comp={comp} id={'a' * 32}"}}

    def test_free_vmid_passes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            red01.red01_vmid_gate(_plan(_comp(td, "assume_breach 1\n")))

    def test_foreign_occupant_refused_and_owner_named(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            comp = _comp(td, "assume_breach 1\n")
            with patch("infra.red01.proxmox_api",
                       return_value=self._marker_desc("other-comp")):
                with self.assertRaises(SystemExit) as cm:
                    red01.red01_vmid_gate(_plan(comp, vms=[_vm(1470)]))
            self.assertIn("already occupied", str(cm.exception))
            self.assertIn("comp=other-comp", str(cm.exception))
            self.assertIn("2026-10-12", str(cm.exception))

    def test_unmarked_occupant_refused_with_check_by_hand(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            comp = _comp(td, "assume_breach 1\n")
            with patch("infra.red01.proxmox_api", return_value={"data": {}}):
                with self.assertRaises(SystemExit) as cm:
                    red01.red01_vmid_gate(_plan(comp, vms=[_vm(1470)]))
            self.assertIn("NO bad-auto marker", str(cm.exception))

    def test_our_marker_on_resume_is_allowed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            comp = _comp(td, "assume_breach 1\n")
            with patch("infra.red01.proxmox_api",
                       return_value=self._marker_desc(comp.name)):
                red01.red01_vmid_gate(_plan(comp, vms=[_vm(1470)], resuming=True))

    def test_our_marker_on_a_fresh_deploy_still_refuses(self):
        """A leftover red01 from a prior run of the same comp id is for the
        teardown to clear, not for this deploy to reuse."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            comp = _comp(td, "assume_breach 1\n")
            with patch("infra.red01.proxmox_api",
                       return_value=self._marker_desc(comp.name)):
                with self.assertRaises(SystemExit):
                    red01.red01_vmid_gate(_plan(comp, vms=[_vm(1470)], resuming=False))


class IpGate(unittest.TestCase):
    def _scan(self, comp, addrs_by_vmid, resuming=False):
        vms = [_vm(v) for v in addrs_by_vmid]
        with patch("infra.red01.guest_ipv4s",
                   side_effect=lambda vm: addrs_by_vmid[vm["vmid"]]):
            red01.red01_ip_gate(_plan(comp, vms=vms, resuming=resuming))

    def test_free_ip_passes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self._scan(_comp(td, "assume_breach 1\n"), {100: ["10.0.0.50"]})

    def test_answering_guest_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                self._scan(_comp(td, "assume_breach 1\n"),
                           {100: ["10.0.0.50"], 200: ["10.0.0.198"]})
            self.assertIn("10.0.0.198", str(cm.exception))
            self.assertIn("already answered", str(cm.exception))

    def test_our_red01_holding_the_ip_on_resume_is_allowed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self._scan(_comp(td, "assume_breach 1\n"),
                       {1470: ["10.0.0.198"], 100: ["10.0.0.50"]}, resuming=True)

    def test_unverifiable_default_ip_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with patch("infra.red01.guest_ipv4s", return_value=None), \
                    patch("infra.red01.proxmox_api", return_value={"data": {}}):
                with self.assertRaises(SystemExit) as cm:
                    red01.red01_ip_gate(
                        _plan(_comp(td, "assume_breach 1\n"), vms=[_vm(100)]))
            self.assertIn("no working agent", str(cm.exception))

    def test_unverifiable_explicit_ip_warns_and_proceeds(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with patch("infra.red01.guest_ipv4s", return_value=None), \
                    patch("infra.red01.proxmox_api", return_value={"data": {}}):
                red01.red01_ip_gate(
                    _plan(_comp(td, "assume_breach 1\nred01_ip 10.9.9.9\n"),
                          vms=[_vm(100)], red01_ip="10.9.9.9", red01_ip_explicit=True))


class GateWiring(unittest.TestCase):
    def test_gate_is_inert_without_assume_breach(self):
        """A comp that deploys no red01 must not have .198 gated on another
        range's legitimately-running red01."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with patch.object(red01, "red01_vmid_gate") as vmid_gate:
                red01.red01_gate(_plan(_comp(td), vms=[_vm(1470)]))
            vmid_gate.assert_not_called()

    def test_single_node_plan_carries_the_reservation(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            comp = _comp(td, "assume_breach 1\nred01_vmid 777\nred01_ip 10.9.9.9\n")
            with patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "pve",
                                         "TEZ_PROXMOX_DATASTORE": "ds"}):
                plan = single_node_plan(comp, BOXES, 1, TEAMS, 1300, True,
                                        "10.0.0.252", None)
            self.assertEqual(plan.red01_vmid, 777)
            self.assertEqual(plan.red01_ip, "10.9.9.9")
            self.assertFalse(plan.resuming)
            # the derived value: one golden stride past slot 0's band
            with patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "pve",
                                         "TEZ_PROXMOX_DATASTORE": "ds"}):
                plan2 = single_node_plan(_comp(td, "assume_breach 1\n"), BOXES, 1,
                                         TEAMS, 1300, False, "10.0.0.252", None)
            self.assertEqual(plan2.red01_vmid, 1460)   # one stride past slot 0's goldens
            self.assertTrue(plan2.resuming)


if __name__ == "__main__":
    unittest.main()
