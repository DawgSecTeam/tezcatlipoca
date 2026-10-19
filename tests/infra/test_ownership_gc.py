"""Orphan-volume GC: a stale content-index entry whose zvol file is already gone
fails the delete task ("unable to open file") and the retry 500s ("got no worker
upid"); that must not abort the whole deploy (live: run-9bd3d844)."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from infra import ownership


class Gc(unittest.TestCase):
    def _api(self, delete):
        def fake(method, path, **kw):
            if method == "GET" and path.endswith("/storage"):
                return {"data": [{"storage": "hdd"}]}
            if method == "GET" and path.endswith("/content"):
                return {"data": [{"vmid": 2557, "volid": "hdd:vm-2557-disk-0"}]}
            if method == "DELETE":
                return delete()
            raise AssertionError((method, path))
        return fake

    def test_a_missing_volume_is_not_fatal(self):
        def boom():
            raise requests.HTTPError("500 Server Error: got no worker upid")
        with patch.object(ownership, "proxmox_api", side_effect=self._api(boom)):
            self.assertEqual(ownership.gc_orphan_volumes("proxmox", 2557), 0)

    def test_a_removed_volume_counts(self):
        with patch.object(ownership, "proxmox_api", side_effect=self._api(lambda: {"data": "UPID:x"})), \
             patch.object(ownership, "wait_for_proxmox_task", return_value=True):
            self.assertEqual(ownership.gc_orphan_volumes("proxmox", 2557), 1)


if __name__ == "__main__":
    unittest.main()
