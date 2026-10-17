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
            if vmid == 1331:
                raise RuntimeError("refusing to destroy vmid 1331: foreign tags")

        with patch.object(golden_ops, "destroy_vm_if_exists", fake_destroy), \
             patch("builtins.print"):
            golden_ops.destroy_golden_set("proxmox", 1180, 3,
                                          expect_tags={"tezcatlipoca"})
        self.assertEqual(calls, [1330, 1331, 1332])

    def test_engine_template_foreign_skip_does_not_raise(self):
        def fake_destroy(node, vmid, expect_tags=None):
            raise RuntimeError("refusing to destroy vmid 1320: foreign tags")

        from engine import engine_template_ops
        with patch.object(engine_template_ops, "destroy_vm_if_exists", fake_destroy), \
             patch("builtins.print"):
            engine_template_ops.destroy_engine_template("proxmox", 1180,
                                                 expect_tags={"tezcatlipoca"})


if __name__ == "__main__":
    unittest.main()
