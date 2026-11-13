"""Template preflight cloud-init gate: a template can be tagged `template` and still
have no cloud-init drive (.150 vmid 920 is named `base-debian13-cloudinit` and ships
none), so its linked clones boot with no network or identity and the failure only
surfaces an hour later at wait_for_boxes_ssh. Linux ostypes require the drive;
Windows and every other non-Linux ostype are exempt. Offline; fake API."""

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
import infra.resources as preflight_templates


def _vm(name, vmid=920, node="proxmox"):
    return {"vmid": vmid, "node": node, "name": name, "template": 1,
            "tags": "template;tezcatlipoca"}


def _api(config, fail=False):
    def _call(method, path, **kw):
        if fail:
            raise RuntimeError("api down")
        return {"data": config}
    return _call




class CloudinitGate(unittest.TestCase):
    def _run(self, config, fail=False, name="base-debian13-cloudinit"):
        boxes = [{"name": "db01", "template": name}]
        out = io.StringIO()
        with patch.object(preflight_templates, "proxmox_api", _api(config, fail=fail)), \
             contextlib.redirect_stdout(out):
            n = preflight_templates.cloudinit_gate({name: [_vm(name)]}, boxes)
        return n, out.getvalue()

    def test_one_name_two_templates_refuses(self):
        # .150 lived this: preflight verified base-fedora44 vmid 1016 while the clone
        # path took the stale drive-less 109 of the same name.
        with self.assertRaises(SystemExit) as cm:
            preflight_templates.cloudinit_gate(
                {"base-fedora44": [_vm("base-fedora44", 109), _vm("base-fedora44", 1016)]},
                [{"name": "web01", "template": "base-fedora44"}])
        msg = str(cm.exception)
        self.assertIn("ambiguous", msg)
        self.assertIn("109", msg)
        self.assertIn("1016", msg)


    def test_linux_without_ci_fails_naming_vmid_name_and_fix(self):
        with self.assertRaises(SystemExit) as cm:
            self._run({"ostype": "l26"}, name="base-debian13-cloudinit")
        msg = str(cm.exception)
        self.assertIn("base-debian13-cloudinit", msg)
        self.assertIn("920", msg)
        self.assertIn("base-debian13-cloudinit-fix", msg)

    def test_windows_without_ci_passes(self):
        n, _ = self._run({"ostype": "win11"}, name="base-windows-server")
        self.assertEqual(n, 1)

    def test_unreadable_config_warns_and_does_not_block(self):
        n, out = self._run({"ostype": "l26"}, fail=True)
        self.assertEqual(n, 0)
        self.assertIn("UNVERIFIED", out)


class TemplateVmid(unittest.TestCase):
    def test_accessor(self):
        from golden.golden_target_ops import template_vmid
        self.assertEqual(template_vmid({"x": 109}, "x"), 109)
        self.assertIsNone(template_vmid({}, "x"))
        with self.assertRaises(RuntimeError) as cm:
            template_vmid({"x": [109, 1016]}, "x")
        self.assertIn("ambiguous", str(cm.exception))

    def test_clone_stamps_source_and_parser_reads_it(self):
        # tonight's failure mode: a golden slot holding a full clone of some OTHER vm
        from infra.ownership import full_clone_data
        from golden.golden_target_ops import golden_source_of
        data = full_clone_data(1430, "golden-web01", "CDE-2026", src=1016)
        self.assertEqual(golden_source_of({"description": data["description"]}), 1016)
        self.assertIsNone(golden_source_of({"description": "tezcatlipoca-clone x"}))
        self.assertIsNone(golden_source_of(None))


if __name__ == "__main__":
    unittest.main()
