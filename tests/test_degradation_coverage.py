"""A failed pool snapshot is a degradation: it is the rollback point, so it must reach the ledger."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import infra.lifecycle  # noqa: E402
from infra import lifecycle  # noqa: E402
import utils  # noqa: E402


class SnapshotFailureIsRecorded(unittest.TestCase):

    def setUp(self):
        utils.clear_degradations()
        self.addCleanup(utils.clear_degradations)

    def test_a_failed_snapshot_records_a_degradation(self):
        def failing_api(method, path, **kwargs):
            raise RuntimeError("zfs error: cannot create snapshot 'hdd/vm-1500-disk-0@tz-base': "
                               "out of space")
        with patch.object(lifecycle, "list_snapshots", return_value=set()), \
                patch.object(lifecycle, "proxmox_api", side_effect=failing_api), \
                patch("builtins.print"):
            ok = infra.lifecycle.take_snapshot("proxmox", 1500, "tz-base")
        self.assertFalse(ok)                       # still never raises, by design
        entries = utils.degradations()
        self.assertEqual(len(entries), 1)
        self.assertIn("tz-base", entries[0]["what"])
        self.assertIn("1500", entries[0]["detail"])
        self.assertIn("out of space", entries[0]["detail"])

    def test_a_successful_snapshot_records_nothing(self):
        with patch.object(lifecycle, "list_snapshots", return_value=set()), \
                patch.object(lifecycle, "proxmox_api",
                             return_value={"data": "UPID:pve:0001"}), \
                patch.object(lifecycle, "wait_for_proxmox_task"), \
                patch("builtins.print"):
            ok = infra.lifecycle.take_snapshot("proxmox", 1500, "tz-ready")
        self.assertTrue(ok)
        self.assertEqual(utils.degradations(), [])


if __name__ == "__main__":
    unittest.main()
