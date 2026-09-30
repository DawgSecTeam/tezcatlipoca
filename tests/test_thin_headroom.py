"""Datastore headroom gate: TEZ_THIN_HEADROOM factor for thin-provisioned pools
(cyberrange loadtest-2026-09-29: 8x7 boxes = ~1.9 TB provisioned vs pools with
~900 GB free; ZFS linked clones only allocate written blocks). Offline; fake API."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import config_ops

BOXES = [{"disk_gb": 60}, {"disk_gb": 15}, {}]  # {} -> unset disk counts as 40 GB


def fake_api(avail_gb):
    def _api(method, path, **kw):
        if method == "GET" and "/storage/" in path and path.endswith("/status"):
            return {"data": {"avail": avail_gb * 1024 ** 3}}
        return {"data": None}
    return _api


class ThinHeadroomGate(unittest.TestCase):
    def run_gate(self, avail_gb, thin=None):
        with patch.object(config_ops, "proxmox_api", fake_api(avail_gb)):
            if thin is None:
                config_ops.check_datastore_headroom("proxmox", "hdd", BOXES, 2)
            else:
                with patch.dict("os.environ", {"TEZ_THIN_HEADROOM": thin}):
                    config_ops.check_datastore_headroom("proxmox", "hdd", BOXES, 2)

    def test_provisioned_math_unchanged_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit):
                self.run_gate(avail_gb=100)  # need 230 GB provisioned
            self.run_gate(avail_gb=300)      # passes at factor 1.0

    def test_thin_factor_counts_fraction(self):
        self.run_gate(avail_gb=100, thin="0.25")  # 230 x 0.25 = 57.5 GB counted

    def test_thin_factor_still_rejects_when_counted_exceeds_free(self):
        with self.assertRaises(SystemExit):
            self.run_gate(avail_gb=50, thin="0.25")

    def test_invalid_factor_rejected(self):
        for bad in ("0", "-1", "1.5"):
            with self.assertRaises(SystemExit):
                self.run_gate(avail_gb=1000, thin=bad)

    def test_unset_disk_counts_as_40gb(self):
        with patch.object(config_ops, "proxmox_api", fake_api(1000)):
            with patch.dict("os.environ", {"TEZ_THIN_HEADROOM": "1.0"}):
                # 2 teams x (60+15+40) = 230 GB counted, 1000 GB free: passes and
                # would fail at 40 GB less if the stand-in regressed
                config_ops.check_datastore_headroom("proxmox", "hdd", BOXES, 2)


if __name__ == "__main__":
    unittest.main()
