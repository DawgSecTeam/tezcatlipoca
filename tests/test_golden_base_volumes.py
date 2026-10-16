"""Both golden conversion paths must verify the vm- -> base- volume rename.

A golden backs a linked clone only from a `base-<vmid>-disk-N` volume. The rename after
`POST /template` is asynchronous, and a golden whose rename never landed stays `template=1`
on a plain `vm-` volume, then fails later at the team-box clone with
`HTTP 500 Linked clone feature is not supported for 'hdd:vm-<vmid>-disk-0'`.

The booted path has guarded this since 2026-10-04. The unbooted (DC) path did not, and a
campaign run died at `[4b/8]` on exactly that 500 with `golden-dc01` as the only golden in
the estate still on a `vm-` volume (live 2026-10-15, run-b47cd966). Both now share one guard.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from golden import golden_ops  # noqa: E402


class AwaitBaseVolumes(unittest.TestCase):
    def _api(self, configs):
        """configs: list of config dicts, handed out one per GET (last one repeats)."""
        state = {"i": 0}

        def api(method, path, **kw):
            assert method == "GET" and path.endswith("/config"), (method, path)
            cfg = configs[min(state["i"], len(configs) - 1)]
            state["i"] += 1
            return {"data": cfg}
        return api

    def test_a_converted_golden_returns_without_sleeping(self):
        api = self._api([{"sata0": "hdd:base-2350-disk-0,size=60G",
                          "ide2": "hdd:vm-2350-cloudinit,media=cdrom"}])
        with patch.object(golden_ops, "proxmox_api", api), \
                patch.object(golden_ops.time, "sleep", lambda *_: self.fail("must not wait")):
            golden_ops._await_base_volumes("n", 2350, "dc01")

    def test_a_rename_that_lands_after_a_poll_is_accepted(self):
        # The conversion is asynchronous: the first read still shows the vm- volume.
        api = self._api([{"sata0": "hdd:vm-2350-disk-0,size=60G"},
                         {"sata0": "hdd:base-2350-disk-0,size=60G"}])
        slept = []
        with patch.object(golden_ops, "proxmox_api", api), \
                patch.object(golden_ops.time, "sleep", slept.append):
            golden_ops._await_base_volumes("n", 2350, "dc01")
        self.assertEqual(len(slept), 1)

    def test_a_lost_rename_raises_and_names_the_golden(self):
        api = self._api([{"sata0": "hdd:vm-2350-disk-0,size=60G"}])
        with patch.object(golden_ops, "proxmox_api", api), \
                patch.object(golden_ops.time, "sleep", lambda *_: None):
            with self.assertRaises(RuntimeError) as ctx:
                golden_ops._await_base_volumes("n", 2350, "dc01")
        self.assertIn("dc01", str(ctx.exception))
        self.assertIn("base- volumes", str(ctx.exception))
        self.assertIn("Linked clone", str(ctx.exception))

    def test_the_cloudinit_drive_is_not_treated_as_a_disk(self):
        # ide2's volume is never renamed; counting it would make every golden look lost.
        api = self._api([{"ide2": "hdd:vm-2350-cloudinit,media=cdrom",
                          "scsi0": "hdd:base-2350-disk-0,size=60G"}])
        with patch.object(golden_ops, "proxmox_api", api), \
                patch.object(golden_ops.time, "sleep", lambda *_: self.fail("must not wait")):
            golden_ops._await_base_volumes("n", 2350, "dc01")

    def test_both_conversion_paths_call_the_guard(self):
        # The regression is a MISSING call, so pin the call sites, not just the helper.
        src = (_REPO / "golden/golden_ops.py").read_text()
        calls = [l for l in src.splitlines() if l.strip().startswith("_await_base_volumes(node")]
        self.assertEqual(len(calls), 2,
                         "both the booted and the unbooted conversion must await the rename")


if __name__ == "__main__":
    unittest.main()
