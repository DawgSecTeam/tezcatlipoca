"""Ownership tags are Proxmox tags: a competition display name with a space
("CDE 2026") must not be written as an un-taggable string and then refused by
the teardown (live: cde-2026 deploy, 2026-10-19)."""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from common.constants import ownership_tags


class Tags(unittest.TestCase):
    def test_a_spaced_name_is_slugged(self):
        self.assertIn("comp-cde-2026", ownership_tags("CDE 2026", "run-x"))

    def test_a_plain_name_is_unchanged(self):
        self.assertIn("comp-scrim-one", ownership_tags("scrim-one", "run-x"))


if __name__ == "__main__":
    unittest.main()
