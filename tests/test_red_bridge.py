"""The private per-run red segment: one bridge + NIC, never the shared management L2.

Every range's red01 uses the same gateway (10.200.0.1) and red01 address
(10.200.0.10). On the shared management L2 a second concurrent range's engine
answers red01's ARP for the gateway, and red's traffic to its teams then goes to
a range with no route to those subnets. The segment rides a bridge whose name is
this run's own.
"""

import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from common.constants import red_bridge_name  # noqa: E402
from common.utils import stages_red  # noqa: E402


def _comp():
    d = Path(tempfile.mkdtemp(prefix="tz-redbridge-")) / "comp"
    d.mkdir(parents=True)
    (d / "Compfile").write_text("name t\ndifficulty 4\n")
    return d


class Naming(unittest.TestCase):
    def test_the_bridge_name_is_the_runs_own_hex(self):
        self.assertEqual(red_bridge_name("run-f00dcafe"), "vmbrRf00dcafe")

    def test_the_name_fits_pves_15_char_cap(self):
        self.assertLessEqual(len(red_bridge_name("run-" + "f" * 16)), 15)

    def test_two_runs_do_not_share_a_bridge(self):
        self.assertNotEqual(red_bridge_name("run-f00dcafe"), red_bridge_name("run-notthis"))


class StagesRed(unittest.TestCase):
    def test_every_range_stages_red(self):
        self.assertTrue(stages_red(_comp()))


if __name__ == "__main__":
    unittest.main()
