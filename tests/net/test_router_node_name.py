"""The engine's tailnet router name is derived from the competition name; a
display name with a space must not become an invalid tailscale hostname
(live: "CDE 2026" -> eng-CDE 2026 -> tailscale up rc=1)."""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from net.remote_access_ops import router_node_name


class Name(unittest.TestCase):
    def test_a_spaced_display_name_is_slugged(self):
        self.assertEqual(router_node_name("CDE 2026"), "eng-cde-2026")

    def test_a_plain_id_is_kept(self):
        self.assertEqual(router_node_name("scrim-one"), "eng-scrim-one")


if __name__ == "__main__":
    unittest.main()
