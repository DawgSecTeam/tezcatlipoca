"""The unbooted DC clone is booted by boot_win in phase 4, before the domain
chain runs on it; its AD feature install has to happen there, not on the golden
(live: ad01 clone inherited InstallPending features, 2026-10-19)."""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from deploy_lib.phases import golden


class BootWin(unittest.TestCase):
    def test_an_ad_box_gets_its_features(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "box_services.json").write_text(json.dumps({"ad01": ["ADDS"]}))
        ctx = SimpleNamespace(comp_dir=d, box_password="pw", node="n")
        seen = {}

        def fake_boot(*a, **k):
            seen.update(k)

        with patch.object(golden, "bootstrap_windows_box", side_effect=fake_boot):
            golden.boot_win(ctx, {"ip": "192.168.1.2", "vmid": 100, "identifier": 1,
                                  "name": "ad01"})
        self.assertEqual(seen.get("features"),
                         ("AD-Domain-Services", "RSAT-AD-PowerShell"))


if __name__ == "__main__":
    unittest.main()
