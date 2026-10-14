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


if __name__ == "__main__":
    unittest.main()
