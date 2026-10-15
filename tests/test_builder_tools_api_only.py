"""The template builders' bootstrap work goes through the Proxmox API, not node shell.

`tools/build-vyos-provision-template.py` fetched its ISO with a node-side `curl` and
`tools/build-pfsense-provision-template.py` renamed templates with `qm set`; both needed
`root@<node>` for work the REST API already does (`download-url`, `PUT .../config`).
What still uses the node hop is the guest bootstrap channel, and these tests pin that
line: the ISO and the rename must never shell out again.

Offline; the Proxmox API is scripted and no VM is touched.
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _REPO / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vyos = _load("vyos_build_under_test", "build-vyos-provision-template.py")
pfsense = _load("pfsense_build_under_test", "build-pfsense-provision-template.py")


def _no_shell(*args, **kwargs):
    raise AssertionError(f"node shell was used: {args} {kwargs}")


class _Api:
# Scripted proxmox_api: records calls, answers the two read shapes the tools use.

    def __init__(self, iso_content=(), config=None):
        self.calls = []
        self.iso_content = list(iso_content)
        self.config = config or {}

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/content"):
            return {"data": self.iso_content}
        if path.endswith("/download-url"):
            return {"data": "UPID:pve:00000001"}
        if path.endswith("/config") and method == "GET":
            return {"data": dict(self.config)}
        if path.endswith("/template"):
            return {"data": "UPID:pve:00000002"}
        return {"data": None}

    def paths(self):
        return [(m, p) for m, p, _ in self.calls]


class VyosIsoViaApi(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"TF_VAR_proxmox_node": "pve"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_present_iso_is_not_downloaded_again(self):
        api = _Api([{"volid": f"local:iso/{vyos.ISO_NAME}", "content": "iso"}])
        with patch.object(vyos, "proxmox_api", api), \
                patch.object(vyos, "wait_for_proxmox_task", _no_shell), \
                patch.object(vyos.subprocess, "run", _no_shell):
            vyos.download()
        self.assertEqual([p for _, p in api.paths() if p.endswith("/download-url")], [])

    def test_download_url_api_and_task_wait(self):
        api = _Api()
        with patch.object(vyos, "proxmox_api", api), \
                patch.object(vyos, "wait_for_proxmox_task") as task, \
                patch.object(vyos.subprocess, "run", _no_shell):
            vyos.download()
        (call,) = [c for c in api.calls if c[1].endswith("/download-url")]
        self.assertEqual(call[0], "POST")
        self.assertEqual(call[1], f"/nodes/pve/storage/{vyos.ISO_STORAGE}/download-url")
        self.assertEqual(call[2]["data"], {"content": "iso", "filename": vyos.ISO_NAME,
                                           "url": vyos.ISO_URL})
        task.assert_called_once_with("pve", "UPID:pve:00000001")

    def test_create_refuses_without_the_iso(self):
        api = _Api()
        with patch.object(vyos, "proxmox_api", api), \
                patch.object(vyos.subprocess, "run", _no_shell):
            with self.assertRaises(SystemExit) as caught:
                vyos.create()
        self.assertIn(vyos.ISO_NAME, str(caught.exception))

    def test_create_boots_the_iso_from_its_storage_volid(self):
        api = _Api([{"volid": f"{vyos.ISO_STORAGE}:iso/{vyos.ISO_NAME}", "content": "iso"}])
        with patch.object(vyos, "proxmox_api", api), \
                patch.object(vyos.subprocess, "run", _no_shell):
            vyos.create()
        (created,) = [c for c in api.calls if c[1] == "/nodes/pve/qemu"]
        self.assertEqual(created[2]["data"]["ide2"],
                         f"{vyos.ISO_STORAGE}:iso/{vyos.ISO_NAME},media=cdrom")


class PfsenseRenameViaApi(unittest.TestCase):
    def test_seal_renames_by_config_and_only_touches_the_node_for_the_alias(self):
        api = _Api(config={"scsi0": "base-958-disk-0"})
        shell_calls = []
        with patch.dict(os.environ, {"TF_VAR_proxmox_node": "pve"}), \
                patch.object(pfsense, "proxmox_api", api), \
                patch.object(pfsense, "wait_for_proxmox_task") as task, \
                patch.object(pfsense, "_halt_and_wait", lambda n: None), \
                patch.object(pfsense, "_node_ssh",
                             lambda cmd, timeout=30: shell_calls.append(cmd)):
            pfsense.agent_seal()
        renames = {c[2]["data"]["name"]: c[1] for c in api.calls
                   if c[0] == "PUT" and "name" in c[2].get("data", {})}
        self.assertEqual(renames, {
            pfsense.QGA_BACKUP_NAME: f"/nodes/pve/qemu/{pfsense.NEWID}/config",
            pfsense.NAME: f"/nodes/pve/qemu/{pfsense.QGA_VMID}/config",
        })
        self.assertEqual(len(shell_calls), 1, "the rename must not need node shell")
        self.assertIn("ip addr del", shell_calls[0])
        task.assert_called_once_with("pve", "UPID:pve:00000002")


if __name__ == "__main__":
    unittest.main()
