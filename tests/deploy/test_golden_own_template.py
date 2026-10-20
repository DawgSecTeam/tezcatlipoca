"""A kept golden carries the SLUGGED comp tag ownership_tags writes; the rebuild
gate's adoption proof compared a raw "comp-CDE-2026" and therefore refused to
reclaim the competition's own template (live: cde-2026 phase 4, run-46dcedd4)."""

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from deploy_lib.golden_plan import _is_own_golden_template


def _cfg(tags, template=1):
    return {"data": {"template": template, "tags": tags}}


class OwnGolden(unittest.TestCase):
    def _check(self, tags, comp_name="CDE-2026", template=1):
        with mock.patch("infra.client.proxmox_api", return_value=_cfg(tags, template)):
            return _is_own_golden_template("proxmox", 2557, comp_name)

    def test_a_cased_comp_name_matches_the_slugged_tag(self):
        self.assertTrue(self._check("comp-cde-2026;run-53eacf63;tezcatlipoca;"
                                   "tezcatlipoca-golden"))

    def test_another_competitions_golden_is_not_adopted(self):
        self.assertFalse(self._check("comp-scrim-one;tezcatlipoca;tezcatlipoca-golden"))

    def test_a_plain_vm_is_not_adopted(self):
        self.assertFalse(self._check("comp-cde-2026;tezcatlipoca;tezcatlipoca-golden",
                                     template=0))


if __name__ == "__main__":
    unittest.main()
