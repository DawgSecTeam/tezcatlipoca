"""Ownership tags are Proxmox tags: a competition display name with a space
("CDE 2026") must not be written as an un-taggable string and then refused by
the teardown (live: cde-2026 deploy, 2026-10-19)."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from common.constants import ownership_tags
from infra import ownership


class Tags(unittest.TestCase):
    def test_a_spaced_name_is_slugged(self):
        self.assertIn("comp-cde-2026", ownership_tags("CDE 2026", "run-x"))

    def test_a_plain_name_is_unchanged(self):
        self.assertIn("comp-scrim-one", ownership_tags("scrim-one", "run-x"))


class Occupancy(unittest.TestCase):
    def test_a_tagged_vm_is_owned_not_missing(self):
        # The config's tag string must reach ownership_verdict raw: re-parsing the parsed set
        # produced "{'comp-c1', ...}" and every template destroy read as another run's.
        def api(method, path, **kw):
            if path.endswith("/qemu"):
                return {"data": [{"vmid": 1151, "name": "golden-web01"}]}
            if path.endswith("/config"):
                return {"data": {"tags": "tezcatlipoca;comp-c1;run-abc;tezcatlipoca-golden"}}
            raise AssertionError(path)

        expect = ownership_tags("c1", "run-abc", "tezcatlipoca-golden")
        with patch.object(ownership, "proxmox_api", side_effect=api):
            verdict, tags, _vm = ownership.vm_occupancy("n", 1151, expect)
        self.assertEqual(verdict, ownership.OWNED)
        self.assertIn("run-abc", tags)


if __name__ == "__main__":
    unittest.main()
