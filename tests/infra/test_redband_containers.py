"""The red-band scan must read a container's address, not call it uncheckable.

`running_guest_ips` asks each running guest for its IPv4s and adds the ones it cannot ask to an
`unchecked` list; `select_red_ip` refuses the whole band when anything is unchecked. Containers
have no guest agent, so before this they were ALL uncheckable and a red-stage deploy could not pick
an address on any host that runs containers:

    RuntimeError: no free address in the red band for red01
    (uncheckable: ['137 (testing-nix on proxmox)', '201 (litellm on proxmox)', ... 15])

(run-14e4d785, phase 6). A container declares its address in its own config, so read it there.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from infra import collisions  # noqa: E402


class GuestIpv4sTest(unittest.TestCase):
    def _api(self, config=None, agent=None):
        def fake(method, path, **kwargs):
            if "/agent/" in path:
                if agent is None:
                    raise RuntimeError("no guest agent")
                return {"data": {"result": agent}}
            return {"data": config or {}}
        return fake

    def test_a_container_reads_its_declared_address(self):
        vm = {"vmid": 201, "node": "proxmox", "type": "lxc", "status": "running"}
        cfg = {"hostname": "litellm", "net0": "name=eth0,bridge=vmbr0,ip=10.0.0.212/24,gw=10.0.0.1"}
        with patch.object(collisions, "proxmox_api", self._api(cfg)):
            self.assertEqual(collisions.guest_ipv4s(vm), ["10.0.0.212"])

    def test_a_container_with_several_nics_reports_every_address(self):
        vm = {"vmid": 9, "node": "proxmox", "type": "lxc", "status": "running"}
        cfg = {"net0": "name=eth0,bridge=vmbr0,ip=10.0.0.240/24",
               "net1": "name=eth1,bridge=vmbr1,ip=192.168.9.5/24"}
        with patch.object(collisions, "proxmox_api", self._api(cfg)):
            self.assertEqual(sorted(collisions.guest_ipv4s(vm)), ["10.0.0.240", "192.168.9.5"])

    def test_a_container_on_dhcp_with_no_agent_stays_unknown(self):
        vm = {"vmid": 9, "node": "proxmox", "type": "lxc", "status": "running"}
        cfg = {"net0": "name=eth0,bridge=vmbr0,ip=dhcp"}
        with patch.object(collisions, "proxmox_api", self._api(cfg)):
            self.assertIsNone(collisions.guest_ipv4s(vm),
                              "a dhcp container with no agent is genuinely unknown")

    def test_a_vm_falls_back_to_its_cloud_init_address(self):
        vm = {"vmid": 1420, "node": "proxmox", "type": "qemu", "status": "running"}
        cfg = {"ipconfig0": "ip=10.0.0.240/24,gw=10.0.0.1"}
        with patch.object(collisions, "proxmox_api", self._api(cfg)):
            self.assertEqual(collisions.guest_ipv4s(vm), ["10.0.0.240"])

    def test_the_agent_still_wins_for_a_vm(self):
        vm = {"vmid": 5, "node": "proxmox", "type": "qemu", "status": "running"}
        agent = [{"ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "10.0.0.241"}]}]
        with patch.object(collisions, "proxmox_api", self._api({"ipconfig0": "ip=dhcp"}, agent)):
            self.assertEqual(collisions.guest_ipv4s(vm), ["10.0.0.241"])

    def test_a_container_outside_the_band_no_longer_blocks_selection(self):
        # The regression in full: containers holding low addresses used to poison the band.
        vms = [{"vmid": v, "node": "proxmox", "type": "lxc", "status": "running", "name": f"c{v}"}
               for v in (137, 201, 202, 210, 218)]
        configs = {v: {"net0": f"name=eth0,bridge=vmbr0,ip=10.0.0.{v}/24"} for v in (137, 201, 202, 210, 218)}

        def fake(method, path, **kwargs):
            if "/agent/" in path:
                raise RuntimeError("no guest agent")
            vmid = int(path.split("/")[4])
            return {"data": configs[vmid]}

        with patch.object(collisions, "proxmox_api", fake):
            ip, unchecked = collisions.select_red_ip(vms)
        self.assertEqual(unchecked, [], "containers must no longer be uncheckable")
        self.assertEqual(ip, "10.0.0.240", "the lowest free red-band address")
