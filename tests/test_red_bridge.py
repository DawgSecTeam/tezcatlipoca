"""The private per-run red segment: one bridge + NIC, never the shared management L2.

Every range's routed red01 uses the same gateway (10.200.0.1) and red01 address (10.200.0.10).
On the shared management L2 a second concurrent range's engine answers red01's ARP for the
gateway, and red's traffic to its teams then goes to a range with no route to those subnets —
the seed's "unreachable over SSH" (caught live: a campaign range's red01 sharing vmbr0 with
a fourth session's). These pin the fix: the segment rides a bridge whose name is this run's own.
"""

import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from common.constants import red_bridge_name  # noqa: E402
from common.utils import stages_red  # noqa: E402
from red import badauto_config_ops as bco  # noqa: E402


def _comp(body=""):
    d = Path(tempfile.mkdtemp(prefix="tz-redbridge-")) / "comp"
    d.mkdir(parents=True)
    (d / "Compfile").write_text(f"name t\n{body}")
    return d


class Naming(unittest.TestCase):
    def test_the_bridge_name_is_the_runs_own_hex(self):
        self.assertEqual(red_bridge_name("run-f00dcafe"), "vmbrRf00dcafe")

    def test_the_name_fits_pves_15_char_cap(self):
        self.assertLessEqual(len(red_bridge_name("run-" + "f" * 16)), 15)

    def test_two_runs_do_not_share_a_bridge(self):
        self.assertNotEqual(red_bridge_name("run-f00dcafe"), red_bridge_name("run-notthis"))


class StagesRed(unittest.TestCase):
    def test_a_v1_assume_breach_range_stages_red(self):
        self.assertTrue(stages_red(_comp("assume_breach 1\n")))

    def test_a_v2_red_engine_range_stages_red(self):
        self.assertTrue(stages_red(_comp("red.engine tlaloc\nred.assume_breach full\n")))

    def test_a_range_without_red_does_not(self):
        self.assertFalse(stages_red(_comp("difficulty 4\n")))


class Config(unittest.TestCase):
    def test_the_red_keys_are_written_when_a_bridge_is_given(self):
        d = _comp("assume_breach 1\nred01_ip 10.0.0.240\n")
        bco.prepare_deploy_config(d, 1300, engine_ip="10.0.0.235",
                                  red_bridge=red_bridge_name("run-f00dcafe"))
        deploy = bco.load_config(d)["deploy"]
        self.assertEqual(deploy["red_bridge"], "vmbrRf00dcafe")
        self.assertEqual(deploy["red_subnet"], "10.200.0.0/24")
        self.assertEqual(deploy["red_seg_ip"], "10.200.0.10")
        self.assertEqual(deploy["red_mode"], "routed")

    def test_no_bridge_leaves_bad_autos_own_defaults_alone(self):
        d = _comp("assume_breach 1\nred01_ip 10.0.0.240\n")
        bco.prepare_deploy_config(d, 1300, engine_ip="10.0.0.235")
        self.assertNotIn("red_bridge", bco.load_config(d)["deploy"])


if __name__ == "__main__":
    unittest.main()
