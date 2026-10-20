"""The collision preflight compares the comp tag against tags written by
ownership_tags (slugged); a raw f-string refused this competition's own kept
templates (live: cde-2026 fresh deploy over its kept goldens)."""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from common.constants import comp_tag


class Tag(unittest.TestCase):
    def test_a_spaced_or_cased_name_matches_the_stored_tag(self):
        self.assertEqual(comp_tag("CDE-2026"), "comp-cde-2026")
        self.assertEqual(comp_tag("CDE 2026"), "comp-cde-2026")

    def test_a_plain_name_is_unchanged(self):
        self.assertEqual(comp_tag("scrim-one"), "comp-scrim-one")


if __name__ == "__main__":
    unittest.main()
