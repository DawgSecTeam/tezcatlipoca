"""Datastore headroom gate: TEZ_THIN_HEADROOM factor for thin-provisioned pools
(a loadtest: 8x7 boxes = ~1.9 TB provisioned vs pools with
~900 GB free; ZFS linked clones only allocate written blocks). Offline; fake API."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
import infra.resources as preflight_headroom

BOXES = [{"disk_gb": 60}, {"disk_gb": 15}, {}]  # {} -> unset disk counts as 40 GB
# team clones = 2 teams x 115 = 230 GB (thin-scalable); full-clone artifacts (one golden per
# box type, engine template, engine, red01) = 115 + 40 + 40 + 60 = 255 GB (not thin).
TEAM_GB, FIXED_GB = 230, 255


def fake_api(avail_gb):
    def _api(method, path, **kw):
        if method == "GET" and "/storage/" in path and path.endswith("/status"):
            return {"data": {"avail": avail_gb * 1024 ** 3}}
        return {"data": None}
    return _api


class ThinHeadroomGate(unittest.TestCase):
    def run_gate(self, avail_gb, thin=None):
        with patch.object(preflight_headroom, "proxmox_api", fake_api(avail_gb)):
            if thin is None:
                preflight_headroom.check_datastore_headroom("proxmox", "hdd", BOXES, 2)
            else:
                with patch.dict("os.environ", {"TEZ_THIN_HEADROOM": thin}):
                    preflight_headroom.check_datastore_headroom("proxmox", "hdd", BOXES, 2)

    def test_provisioned_math_unchanged_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit):
                self.run_gate(avail_gb=100)  # needs TEAM_GB + FIXED_GB
            self.run_gate(avail_gb=TEAM_GB + FIXED_GB)

    def test_a_failed_status_read_raises_instead_of_skipping_the_gate(self):
        with patch.object(preflight_headroom, "proxmox_api", side_effect=RuntimeError("500")):
            with self.assertRaises(RuntimeError):
                preflight_headroom.check_datastore_headroom("proxmox", "hdd", BOXES, 2)

    def test_thin_factor_counts_fraction(self):
        # 230 x 0.25 = 57.5 GB of team clones, plus the 255 GB of full-clone artifacts.
        self.run_gate(avail_gb=-(-TEAM_GB * 25 // 100) + FIXED_GB, thin="0.25")

    def test_thin_factor_still_rejects_when_counted_exceeds_free(self):
        with self.assertRaises(SystemExit):
            self.run_gate(avail_gb=FIXED_GB - 1, thin="0.25")

    def test_invalid_factor_rejected(self):
        for bad in ("0", "-1", "1.5"):
            with self.assertRaises(SystemExit):
                self.run_gate(avail_gb=1000, thin=bad)



if __name__ == "__main__":
    unittest.main()
