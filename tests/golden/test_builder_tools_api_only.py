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
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
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
        patcher = patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "pve"})
        patcher.start()
        self.addCleanup(patcher.stop)


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



class PfsenseDirectAgentClone(unittest.TestCase):
    def test_it_moves_the_clone_lan_to_the_bootstrap_address(self):
        typed = []
        opts = SimpleNamespace(mode="direct", bootstrap_ip="10.0.0.236")
        with patch.object(pfsense, "proxmox_api", _Api()), \
                patch.object(pfsense, "wait_for_proxmox_task"), \
                patch.object(pfsense.build_ssh, "options", return_value=opts), \
                patch.object(pfsense.time, "sleep"), \
                patch.object(pfsense, "type_line",
                             lambda text, inter_key_s=0.04, vmid=pfsense.NEWID:
                             typed.append((text, vmid))):
            pfsense.agent_clone()
        self.assertTrue(any("10.0.0.236" in text and vmid == pfsense.QGA_VMID
                            for text, vmid in typed))
        self.assertEqual(typed[-1], ("reboot", pfsense.QGA_VMID))

    def test_catalog_entries_reads_the_jsonl_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "packagesite.yaml"
            site.write_text(
                '{"name": "libfoo", "path": "all/libfoo.pkg", "deps": {}}\n'
                '{"name": "qemu-guest-agent", "path": "all/qga.pkg",'
                ' "deps": {"libfoo": "1"}}\n'
                '{"name": "unrelated", "path": "all/x.pkg", "deps": {}}\n')
            found = pfsense._catalog_entries(site, {"qemu-guest-agent", "libfoo"})
        self.assertEqual(sorted(found), ["libfoo", "qemu-guest-agent"])
        self.assertEqual(found["qemu-guest-agent"]["path"], "all/qga.pkg")


class PfsenseRenameViaApi(unittest.TestCase):
    def test_seal_renames_by_config_and_only_touches_the_node_for_the_alias(self):
        api = _Api(config={"scsi0": "base-958-disk-0"})
        shell_calls = []
        with patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "pve",
                                     "TEZ_PROXMOX_ENDPOINT": "https://192.0.2.10:8006",
                                     "TEZ_BUILD_MODE": "node"}), \
                patch.object(pfsense, "proxmox_api", api), \
                patch.object(pfsense, "wait_for_proxmox_task") as task, \
                patch.object(pfsense, "_halt_and_wait", lambda n: None), \
                patch.object(pfsense.build_ssh, "_node_ssh",
                             lambda node, key, cmd, timeout=30: shell_calls.append(cmd)):
            pfsense.agent_seal()
        renames = {c[2]["data"]["name"]: c[1] for c in api.calls
                   if c[0] == "PUT" and "name" in c[2].get("data", {})}
        self.assertEqual(renames, {
            pfsense.QGA_BACKUP_NAME: f"/nodes/pve/qemu/{pfsense.NEWID}/config",
            pfsense.NAME: f"/nodes/pve/qemu/{pfsense.QGA_VMID}/config",
        })
        self.assertEqual(len(shell_calls), 2, "the rename must not need node shell")
        self.assertIn("ip addr add", shell_calls[0])
        self.assertIn("ip addr del", shell_calls[-1])
        task.assert_called_once_with("pve", "UPID:pve:00000002")


if __name__ == "__main__":
    unittest.main()
