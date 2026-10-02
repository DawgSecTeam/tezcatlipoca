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

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import config_ops


def _vm(name, vmid=920, node="proxmox"):
    return {"vmid": vmid, "node": node, "name": name, "template": 1,
            "tags": "template;tezcatlipoca"}


def _api(config, fail=False):
    def _call(method, path, **kw):
        if fail:
            raise RuntimeError("api down")
        return {"data": config}
    return _call


class TemplateCloudinitMissing(unittest.TestCase):
    def test_linux_with_cloudinit_drive_is_fine(self):
        self.assertFalse(config_ops.template_cloudinit_missing(
            {"ostype": "l26", "ide2": "local-lvm:vm-920-cloudinit,media=cdrom"}))
        self.assertFalse(config_ops.template_cloudinit_missing(
            {"ostype": "l26", "scsi0": "local-lvm:vm-920-cloudinit"}))
        self.assertFalse(config_ops.template_cloudinit_missing(
            {"ostype": "l26", "sata1": "local:vm-920-cloudinit"}))

    def test_linux_without_cloudinit_is_missing(self):
        self.assertTrue(config_ops.template_cloudinit_missing(
            {"ostype": "l26", "ide2": "local:iso/debian.iso"}))
        self.assertTrue(config_ops.template_cloudinit_missing({"ostype": "l26"}))

    def test_windows_and_other_ostypes_are_exempt(self):
        for ostype in ("win11", "win2k22", "other", "solaris", ""):
            self.assertFalse(config_ops.template_cloudinit_missing({"ostype": ostype}))


class CloudinitGate(unittest.TestCase):
    def _run(self, config, fail=False, name="base-debian13-cloudinit"):
        boxes = [{"name": "db01", "template": name}]
        out = io.StringIO()
        with patch.object(config_ops, "proxmox_api", _api(config, fail=fail)), \
             contextlib.redirect_stdout(out):
            n = config_ops._cloudinit_gate({name: _vm(name)}, boxes)
        return n, out.getvalue()

    def test_linux_with_ci_passes(self):
        n, out = self._run({"ostype": "l26", "ide2": "local-lvm:vm-920-cloudinit"})
        self.assertEqual(n, 1)
        self.assertIn("cloud-init drive", out)

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


if __name__ == "__main__":
    unittest.main()
