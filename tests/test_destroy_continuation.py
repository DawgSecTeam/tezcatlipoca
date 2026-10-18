"""Teardown continuation : a failed/interrupted destroy must be
completable by re-running the pipeline — a foreign template slot skips and the remaining
slots continue. Offline; fake Proxmox API."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))


from golden import golden_ops  # noqa: E402


class GoldenDestroySkipsForeign(unittest.TestCase):
    def test_foreign_slot_skips_and_remaining_slots_continue(self):
        calls = []

        def fake_destroy(node, vmid, expect_tags=None):
            calls.append(vmid)

        def occupancy(node, vmid, expect_tags):
            if vmid == 1331:
                return "foreign", {"comp-other", "run-x"}, {"name": "someone"}
            return "owned", set(expect_tags), {"name": f"golden-{vmid}"}

        with patch.object(golden_ops, "destroy_vm_if_exists", fake_destroy), \
                patch.object(golden_ops, "vm_occupancy", occupancy), \
                patch("builtins.print"):
            skipped = golden_ops.destroy_golden_set("proxmox", 1180, 3,
                                                    expect_tags={"tezcatlipoca"})
        self.assertEqual(calls, [1330, 1332])
        self.assertEqual(len(skipped), 1)
        self.assertIn("foreign", skipped[0])

    def test_another_run_of_this_competition_is_never_destroyed(self):
        calls = []

        def fake_destroy(node, vmid, expect_tags=None):
            calls.append(vmid)

        def occupancy(node, vmid, expect_tags):
            return ("missing",
                    {"tezcatlipoca", "comp-comp", "run-other"},
                    {"name": "golden-0"})

        with patch.object(golden_ops, "destroy_vm_if_exists", fake_destroy), \
                patch.object(golden_ops, "vm_occupancy", occupancy), \
                patch("builtins.print"):
            skipped = golden_ops.destroy_golden_set(
                "proxmox", 1180, 1,
                expect_tags={"tezcatlipoca", "comp-comp", "run-mine"})
        self.assertEqual(calls, [], "a parallel run's template must not be eaten")
        self.assertTrue(skipped[0].startswith(golden_ops.OTHER_RUN))

    def test_engine_template_foreign_skip_does_not_raise(self):
        from engine import engine_template_ops
        with patch.object(engine_template_ops, "vm_occupancy",
                          return_value=("foreign", {"comp-other"}, {"name": "x"})), \
                patch("builtins.print"):
            skipped = engine_template_ops.destroy_engine_template(
                "proxmox", 1180, expect_tags={"tezcatlipoca"})
        self.assertEqual(len(skipped), 1)


if __name__ == "__main__":
    unittest.main()
