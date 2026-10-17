"""estate.engine_setup: the engine's guest-side network and boot configuration."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from estate import engine_setup  # noqa: E402
from infra import client  # noqa: E402

CTX = {"ssh_key_path": "/tmp/key", "vm_username": "sysadmin", "scoring_engine_ip": "10.0.0.248"}


class Netplan(unittest.TestCase):
    def test_team_nics_are_numbered_from_ens19(self):
        yaml = engine_setup.netplan_yaml(["120", "121"])
        self.assertIn("ens19:", yaml)
        self.assertIn('addresses: ["192.168.120.1/24"]', yaml)
        self.assertIn("ens20:", yaml)
        self.assertIn('addresses: ["192.168.121.1/24"]', yaml)

    def test_transit_nics_append_after_every_team_nic(self):
        yaml = engine_setup.netplan_yaml(["120", "121"], ["120", "121"])
        self.assertIn("ens21:", yaml)
        self.assertIn('addresses: ["172.31.120.1/30"]', yaml)
        self.assertIn("ens22:", yaml)
        self.assertIn('addresses: ["172.31.121.1/30"]', yaml)

    def test_no_teams_yields_no_ethernets(self):
        # An empty ethernets map is a netplan error, so the block is not emitted at all.
        self.assertNotIn("ens", engine_setup.netplan_yaml([]))

    def test_the_red_segment_nic_appends_after_the_transits(self):
        # red01's segment gateway lives on the range's private red bridge; the engine holds
        # it on that NIC, never as a second address on the management interface.
        yaml = engine_setup.netplan_yaml(["120"], ["120"],
                                         {"subnet": "10.200.0.0/24", "gw": "10.200.0.1"})
        self.assertIn("ens19:", yaml)
        self.assertIn('addresses: ["192.168.120.1/24"]', yaml)
        self.assertIn("ens20:", yaml)
        self.assertIn('addresses: ["172.31.120.1/30"]', yaml)
        self.assertIn("ens21:", yaml)
        self.assertIn('addresses: ["10.200.0.1/24"]', yaml)

    def test_no_red_segment_adds_no_extra_nic(self):
        yaml = engine_setup.netplan_yaml(["120"], ["120"])
        self.assertNotIn("ens21:", yaml)
        self.assertNotIn("10.200.0.1", yaml)


class Base(unittest.TestCase):
    def setUp(self):
        self.commands = []

        def fake_ssh(ctx, cmd, timeout=30):
            self.commands.append(cmd)
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        # wait_for_proxmox_task resolves proxmox_api in infra.client's namespace, so the
        # task-status shape has to come from the same fake.
        def fake_api(method, path, **kwargs):
            if "/tasks/" in path:
                return {"data": {"status": "stopped", "exitstatus": "OK"}}
            return {"data": "UPID:x"}

        for patch in (
            mock.patch.object(engine_setup, "ssh_on_gateway", side_effect=fake_ssh),
            mock.patch.object(engine_setup, "wait_for_ssh", return_value=True),
            mock.patch.object(engine_setup.time, "sleep"),
            mock.patch.object(engine_setup, "proxmox_api", side_effect=fake_api),
            mock.patch.object(client, "proxmox_api", side_effect=fake_api),
        ):
            patch.start()
            self.addCleanup(patch.stop)


class Network(Base):
    def test_writes_netplan_and_sysctls(self):
        engine_setup.configure_engine_network(CTX, ["120", "121"])
        joined = "\n".join(self.commands)
        self.assertIn("/etc/netplan/60-team-ifaces.yaml", joined)
        self.assertIn("chmod 600", joined)
        self.assertIn("netplan apply", joined)
        self.assertIn("net.ipv4.ip_forward=1", joined)
        self.assertIn("sudo sysctl -p", joined)

    def test_all_satellite_spread_skips_the_netplan_block(self):
        engine_setup.configure_engine_network(CTX, [])
        joined = "\n".join(self.commands)
        self.assertNotIn("netplan", joined)
        self.assertIn("net.ipv4.ip_forward=1", joined)

    def test_satellite_routes_are_applied_and_persisted(self):
        engine_setup.configure_engine_network(
            CTX, ["120"], satellite_routes=[{"subnet": "192.168.121.0/24", "via": "10.0.0.249"}])
        joined = "\n".join(self.commands)
        self.assertIn("ip route replace 192.168.121.0/24 via 10.0.0.249", joined)
        self.assertIn("satellite-routes.service", joined)
        self.assertIn("systemctl enable --now satellite-routes.service", joined)

    def test_without_satellites_no_route_unit_is_written(self):
        engine_setup.configure_engine_network(CTX, ["120"])
        self.assertNotIn("satellite-routes.service", "\n".join(self.commands))

    def test_a_failing_command_raises(self):
        with mock.patch.object(engine_setup, "ssh_on_gateway",
                               return_value=type("R", (), {"returncode": 1, "stdout": "",
                                                           "stderr": "boom"})()):
            with self.assertRaises(RuntimeError) as raised:
                engine_setup.configure_engine_network(CTX, ["120"])
            self.assertIn("boom", str(raised.exception))


class ColdBoot(Base):
    def test_stops_then_starts(self):
        engine_setup.cold_boot("pve", 1300)
        calls = engine_setup.proxmox_api.call_args_list
        paths = [c.args[1] for c in calls]
        self.assertTrue(paths[0].endswith("/qemu/1300/status/stop"))
        self.assertTrue(paths[1].endswith("/qemu/1300/status/start"))


class Readiness(Base):
    def test_raises_when_ssh_never_answers(self):
        with mock.patch.object(engine_setup, "wait_for_ssh", return_value=False):
            with self.assertRaises(RuntimeError) as raised:
                engine_setup.wait_engine_ready(CTX, timeout=1)
            self.assertIn("never accepted SSH", str(raised.exception))

    def test_settles_after_a_successful_wait(self):
        engine_setup.wait_engine_ready(CTX, timeout=1)
        engine_setup.time.sleep.assert_called_once_with(engine_setup.READY_SETTLE_S)


if __name__ == "__main__":
    unittest.main()
