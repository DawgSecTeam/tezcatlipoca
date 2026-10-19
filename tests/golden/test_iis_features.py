"""A box whose services include IIS needs the management module before the plan's
IIS step, or `Import-Module WebAdministration` fails with FileNotFoundException
(live: scrim-one/win02, 2026-10-19). The feature set is read from the comp's
box_services.json so only IIS boxes pay for it."""

import json
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from golden.golden_ops import windows_plan_features


class Features(unittest.TestCase):
    def test_an_iis_box_gets_the_management_tools(self):
        d = Path(self._tmp())
        (d / "box_services.json").write_text(json.dumps(
            {"win02": [{"name": "IIS FTP"}], "web01": ["nginx"]}))
        self.assertEqual(windows_plan_features(d, "win02"), ("Web-Scripting-Tools",))
        self.assertEqual(windows_plan_features(d, "web01"), ())

    def test_an_ad_box_gets_the_ad_modules(self):
        d = Path(self._tmp())
        (d / "box_services.json").write_text(json.dumps(
            {"ad01": ["ADDS", "Enable WinRM"], "db01": ["mysql"]}))
        self.assertEqual(windows_plan_features(d, "ad01"),
                         ("AD-Domain-Services", "RSAT-AD-PowerShell"))
        self.assertEqual(windows_plan_features(d, "db01"), ())

    def test_a_missing_or_broken_file_is_no_features(self):
        d = Path(self._tmp())
        self.assertEqual(windows_plan_features(d, "win02"), ())
        (d / "box_services.json").write_text("{")
        self.assertEqual(windows_plan_features(d, "win02"), ())

    def _tmp(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


if __name__ == "__main__":
    unittest.main()
