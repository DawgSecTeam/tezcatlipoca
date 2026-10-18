"""The student portal's noVNC client is pinned to a release tarball with integrity."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PORTAL = ROOT / "portal" / "frontend"


class NovncPin(unittest.TestCase):
    def test_novnc_is_pinned_to_a_release_tarball_with_integrity(self):
        lock = json.loads((PORTAL / "package-lock.json").read_text())
        entry = lock["packages"]["node_modules/@novnc/novnc"]
        self.assertTrue(entry["resolved"].endswith("/refs/tags/v1.6.0.tar.gz"), entry)
        self.assertTrue(entry["integrity"].startswith("sha512-"))


if __name__ == "__main__":
    unittest.main()
