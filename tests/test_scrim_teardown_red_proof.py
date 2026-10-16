"""Teardown's red01-gone proof must be able to read the live cluster.

Regression for the 2026-10-14 e2e preflight: `_red_vm_still_exists` imported `live_vmids`
from `range_ops`, a module the 0.2.0 rewrite removed. The failure was swallowed into
"could not verify red01 vmid … is gone" and returned None, so the teardown assertion
degraded from "verified gone" to "unverifiable" — and verify_clean could never read clean.
"""

import unittest
from unittest import mock

from scrim import teardown_stage


class RedVmidProof(unittest.TestCase):
    def test_proof_asks_the_cluster(self):
        with mock.patch("infra.client.live_vmids", return_value={999, 1400}) as live:
            self.assertTrue(teardown_stage._red_vm_still_exists(999))
            self.assertFalse(teardown_stage._red_vm_still_exists(1000))
        self.assertEqual(live.call_count, 2)

    def test_no_vmid_is_unverifiable_not_gone(self):
        self.assertIsNone(teardown_stage._red_vm_still_exists(None))

    def test_an_unreadable_cluster_is_unverifiable_not_gone(self):
        with mock.patch("infra.client.live_vmids", side_effect=RuntimeError("api down")):
            self.assertIsNone(teardown_stage._red_vm_still_exists(999))


class Red01Attribution(unittest.TestCase):
    def _verdict(self, description=None, comp_name=None, vms=None, boom=False):
        from infra import red01

        def api(method, path, **kw):
            if boom:
                raise RuntimeError("api down")
            if path == "/cluster/resources":
                return {"data": vms if vms is not None else [
                    {"vmid": 998, "node": "n", "name": "bad-auto-red01", "status": "running"}]}
            return {"data": {"description": description}}

        with mock.patch("infra.red01.proxmox_api", side_effect=api):
            return red01.occupant_verdict(998, comp_name)

    def test_a_bare_vmid_with_a_foreign_marker_is_attributed(self):
        self.assertEqual(self._verdict("bad-auto red01 comp=other id=deadbeef", "mine"),
                         "foreign")

    def test_a_marker_naming_this_competition_is_ours(self):
        self.assertEqual(self._verdict("bad-auto red01 comp=mine id=deadbeef", "mine"), "ours")

    def test_an_unmarked_occupant_is_never_clean(self):
        self.assertEqual(self._verdict("", "mine"), "unmarked")

    def test_an_absent_vmid_is_absent(self):
        self.assertEqual(self._verdict(vms=[]), "absent")

    def test_an_unreadable_cluster_is_unknown(self):
        self.assertEqual(self._verdict(boom=True), "unknown")


if __name__ == "__main__":
    unittest.main()
