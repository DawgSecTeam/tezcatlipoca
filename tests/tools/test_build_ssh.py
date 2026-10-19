"""Offline tests for the template builders' SSH transport.

No test dials a node: `subprocess.run` is a recording fake and each builder's
`proxmox_api` is a fake that only reports VM status/config.
"""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import build_ssh

REPO = Path(__file__).resolve().parents[2]
KEY = Path("/tmp/fake-proxmox-key")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    module = importlib.util.module_from_spec(spec)
    # The builders load the real .env at import; the offline suite must not inherit it.
    with mock.patch("dotenv.load_dotenv"):
        spec.loader.exec_module(module)
    return module


pfsense = _load("build_pfsense_provision_template",
                "tools/build-pfsense-provision-template.py")
vyos = _load("build_vyos_provision_template", "tools/build-vyos-provision-template.py")


def _record_run(calls):
    def run(argv, **kw):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


class NodeMode(unittest.TestCase):
    def test_node_mode_keeps_the_node_hop(self):
        calls = []
        opts = build_ssh.options(["--mode", "node"])
        with mock.patch.object(build_ssh.subprocess, "run", side_effect=_record_run(calls)):
            with build_ssh.aliased(opts, node="node.example", bridge="vmbr7", key=KEY):
                argv = build_ssh.guest_argv(opts, node="node.example", key=KEY,
                                            user="admin", command="echo ok")
        self.assertEqual(calls[0][-2], "root@node.example")
        self.assertIn("ip addr add 192.168.1.2/24 dev vmbr7", calls[0][-1])
        self.assertEqual(calls[-1][-2], "root@node.example")
        self.assertIn("ip addr del 192.168.1.2/24 dev vmbr7", calls[-1][-1])
        self.assertTrue(any("ProxyCommand=" in arg for arg in argv))
        self.assertEqual(argv[-2], "admin@192.168.1.1")


class OperatorMode(unittest.TestCase):
    def test_operator_mode_never_ssh_roots_the_node_and_cleans_up(self):
        calls = []
        opts = build_ssh.options(["--mode", "operator", "--operator-iface", "eth9"])
        with mock.patch.object(build_ssh.subprocess, "run", side_effect=_record_run(calls)):
            with build_ssh.aliased(opts, node="node.example", bridge="vmbr7", key=KEY):
                pass
        self.assertFalse([argv for argv in calls if argv[0] == "ssh"])
        self.assertEqual(calls, [
            ["sudo", "-n", "ip", "addr", "add", "192.168.1.2/24", "dev", "eth9"],
            ["sudo", "-n", "ip", "addr", "del", "192.168.1.2/24", "dev", "eth9"],
        ])
        argv = build_ssh.guest_argv(opts, node="node.example", key=KEY, user="admin")
        self.assertEqual(argv[-1], "admin@192.168.1.1")
        self.assertFalse(any("ProxyCommand=" in arg for arg in argv))

    def test_operator_alias_is_removed_even_when_the_body_fails(self):
        calls = []
        opts = build_ssh.options(["--mode", "operator", "--operator-iface", "eth9"])
        with mock.patch.object(build_ssh.subprocess, "run", side_effect=_record_run(calls)):
            with self.assertRaises(RuntimeError):
                with build_ssh.aliased(opts, node="node.example", bridge="vmbr7", key=KEY):
                    raise RuntimeError("transport failed")
        self.assertEqual(calls[-1],
                         ["sudo", "-n", "ip", "addr", "del", "192.168.1.2/24",
                          "dev", "eth9"])


class DirectMode(unittest.TestCase):
    def test_direct_mode_never_aliases(self):
        opts = build_ssh.options(["--mode", "direct", "--bootstrap-ip", "10.9.9.5"])
        with mock.patch.object(build_ssh.subprocess, "run") as run:
            with build_ssh.aliased(opts, node="node.example", bridge="vmbr7", key=KEY):
                pass
        run.assert_not_called()
        argv = build_ssh.guest_argv(opts, node="node.example", key=KEY, user="admin")
        self.assertEqual(argv[-1], "admin@10.9.9.5")
        self.assertFalse(any("ProxyCommand=" in arg for arg in argv))

    def test_direct_mode_requires_a_bootstrap_address(self):
        with mock.patch.dict(os.environ, {"TEZ_BUILD_BOOTSTRAP_IP": "",
                                          "TEZ_CONFIG": "/nonexistent"}):
            from estate import config
            config.reset()
            self.addCleanup(config.reset)
            with self.assertRaises(SystemExit):
                build_ssh.options(["--mode", "direct"])


class _FakePve:
    def __init__(self, statuses=("stopped",)):
        self.statuses = list(statuses)
        self.calls = []

    def __call__(self, method, path, **kw):
        self.calls.append((method, path))
        if path.endswith("/status/current"):
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return {"data": {"status": status}}
        if method == "GET" and path.endswith("/config"):
            return {"data": {"scsi0": "base-1000-disk-0"}}
        return {"data": "UPID:fake"}


class _DirectBuilderCase(unittest.TestCase):
    bootstrap = "10.9.9.5"

    def setUp(self):
        for key, value in {
            "TEZ_BUILD_MODE": "direct",
            "TEZ_BUILD_BOOTSTRAP_IP": self.bootstrap,
            "TEZ_PROXMOX_NODE": "pve-test",
            "TEZ_PROXMOX_ENDPOINT": "https://192.0.2.10:8006",
        }.items():
            os.environ[key] = value
        from estate import config
        config.reset()
        self.addCleanup(config.reset)


class PfsenseDirect(_DirectBuilderCase):
    def test_edit_types_the_bootstrap_address(self):
        typed = []
        with tempfile.TemporaryDirectory() as tmp:
            pub = Path(tmp) / "proxmox.pub"
            pub.write_text("ssh-ed25519 AAAA test\n")
            with mock.patch.object(pfsense, "PUBKEY_FILE", pub), \
                    mock.patch.object(pfsense, "type_line", lambda text, **kw: typed.append(text)), \
                    mock.patch("time.sleep"):
                pfsense.edit()
        self.assertTrue(any(f"<ipaddr>{self.bootstrap}</ipaddr>" in line for line in typed))

    def test_seal_restores_the_contract_address_before_halting(self):
        typed = []
        pve = _FakePve()
        with mock.patch.object(pfsense, "proxmox_api", pve), \
                mock.patch.object(pfsense, "type_line", lambda text, **kw: typed.append(text)), \
                mock.patch("time.sleep"):
            pfsense.seal()
        restore = [i for i, line in enumerate(typed)
                   if line.startswith(f"sed -i '' -e 's,<ipaddr>{self.bootstrap}</ipaddr>")]
        self.assertTrue(restore, typed)
        self.assertLess(restore[0], typed.index("halt -p"))
        self.assertIn("192.168.1.1", typed[restore[0]])

    def test_agent_seal_restores_the_contract_on_the_agent_vm(self):
        typed = []
        pve = _FakePve()

        def record(text, **kw):
            typed.append((text, kw.get("vmid")))

        with mock.patch.object(pfsense, "proxmox_api", pve), \
                mock.patch.object(pfsense, "type_line", record), \
                mock.patch.object(pfsense, "wait_for_proxmox_task"), \
                mock.patch("time.sleep"):
            pfsense.agent_seal()
        self.assertTrue(all(vmid == pfsense.QGA_VMID for _, vmid in typed), typed)
        lines = [text for text, _ in typed]
        restore = [i for i, line in enumerate(lines)
                   if line.startswith(f"sed -i '' -e 's,<ipaddr>{self.bootstrap}</ipaddr>")]
        self.assertTrue(restore, typed)
        self.assertLess(restore[0], lines.index("halt -p"))


class VyosDirect(_DirectBuilderCase):
    def test_provision_types_the_bootstrap_address(self):
        typed = []
        with tempfile.TemporaryDirectory() as tmp:
            pub = Path(tmp) / "proxmox.pub"
            pub.write_text("ssh-ed25519 AAAA test\n")
            with mock.patch.object(vyos, "PUBKEY_FILE", pub), \
                    mock.patch.object(vyos, "type_line", lambda text, **kw: typed.append(text)), \
                    mock.patch("time.sleep"):
                vyos.provision()
        self.assertIn(f"set interfaces ethernet eth1 address {self.bootstrap}/24", typed)

    def test_seal_restores_the_contract_address_before_poweroff(self):
        typed = []
        pve = _FakePve(statuses=("running", "stopped"))
        with mock.patch.object(vyos, "proxmox_api", pve), \
                mock.patch.object(vyos, "type_line", lambda text, **kw: typed.append(text)), \
                mock.patch.object(vyos, "wait_for_proxmox_task"), \
                mock.patch("time.sleep"):
            vyos.seal()
        restore = typed.index("set interfaces ethernet eth1 address 192.168.1.1/24")
        self.assertLess(restore, typed.index("poweroff"))
        self.assertLess(typed.index("configure"), restore)

    def test_seal_refuses_a_stopped_vm_before_it_can_restore(self):
        pve = _FakePve(statuses=("stopped",))
        with mock.patch.object(vyos, "proxmox_api", pve), \
                mock.patch("time.sleep"):
            with self.assertRaises(SystemExit):
                vyos.seal()


if __name__ == "__main__":
    unittest.main()
