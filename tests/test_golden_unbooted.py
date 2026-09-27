"""unbooted_golden_boxes: absent role file = no-domain lineup; present-but-invalid
fails closed (SystemExit) so a broken file can never produce a booted shared DC golden."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import golden_ops

BOXES = [{"name": "dc01", "template": "windows-server-2022"},
         {"name": "web01", "template": "ubuntu-2204-web"}]


class UnbootedGoldenBoxesTests(unittest.TestCase):
    def _comp_dir(self, roles_text, with_boxes=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        comp_dir = Path(tmp.name)
        if with_boxes:
            (comp_dir / "boxes.json").write_text(json.dumps(BOXES))
        if roles_text is not None:
            (comp_dir / "domain_roles.json").write_text(roles_text)
        return comp_dir

    def test_absent_file_is_no_domain_lineup(self):
        self.assertEqual(golden_ops.unbooted_golden_boxes(self._comp_dir(None)), set())

    def test_valid_roles_select_dc_boxes_only(self):
        comp_dir = self._comp_dir('{"dc01": "dc", "web01": "member"}')
        self.assertEqual(golden_ops.unbooted_golden_boxes(comp_dir), {"dc01"})

    def test_empty_roles_is_no_domain_lineup(self):
        comp_dir = self._comp_dir('{}')
        self.assertEqual(golden_ops.unbooted_golden_boxes(comp_dir), set())

    def test_malformed_json_raises(self):
        with self.assertRaises(SystemExit):
            golden_ops.unbooted_golden_boxes(self._comp_dir('{"dc01": '))

    def test_non_dict_roles_raise(self):
        with self.assertRaises(SystemExit):
            golden_ops.unbooted_golden_boxes(self._comp_dir('["dc01"]'))

    def test_invalid_role_value_raises(self):
        with self.assertRaises(SystemExit):
            golden_ops.unbooted_golden_boxes(self._comp_dir('{"dc01": "primary-dc"}'))

    def test_unknown_box_name_raises(self):
        with self.assertRaises(SystemExit):
            golden_ops.unbooted_golden_boxes(
                self._comp_dir('{"dc01": "dc", "ghost": "member"}'))

    def test_name_cross_check_skipped_without_boxes_json(self):
        comp_dir = self._comp_dir('{"dc01": "dc"}', with_boxes=False)
        self.assertEqual(golden_ops.unbooted_golden_boxes(comp_dir), {"dc01"})


if __name__ == "__main__":
    unittest.main()
