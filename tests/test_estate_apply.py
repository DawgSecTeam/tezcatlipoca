"""estate.apply: the estate component's creators, against a fake Proxmox API."""

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from estate import apply as estate_apply  # noqa: E402
from estate.state import EstateState  # noqa: E402
from infra import client, lifecycle, ownership as ownership_ops  # noqa: E402
from infra.ownership import Ownership  # noqa: E402

NODE = "pve"


class FakePVE:
    """A Proxmox API stand-in: VM list, network list, VM configs, and call capture."""

    def __init__(self, vms=None, networks=None, configs=None):
        self.vms = vms or []
        self.networks = networks or []
        self.configs = configs or {}
        self.calls = []

    def __call__(self, method, path, **kwargs):
        data = kwargs.get("data")
        self.calls.append((method, path, data))
        if "/tasks/" in path:
            return {"data": {"status": "stopped", "exitstatus": "OK"}}
        if path.endswith("/network"):
            if method == "GET":
                return {"data": [dict(n) for n in self.networks]}
            if method == "POST":
                self.networks.append({"iface": data["iface"], "type": data["type"]})
            return {"data": None}
        if path.endswith("/storage"):
            return {"data": []}
        if re.search(r"/qemu$", path):
            return {"data": [dict(v) for v in self.vms]}
        match = re.match(r"^/nodes/[^/]+/qemu/(\d+)(?:/(.+))?$", path)
        if not match:
            raise AssertionError(f"unexpected path {method} {path}")
        vmid, tail = int(match.group(1)), match.group(2)
        if tail == "clone" and method == "POST":
            self.vms.append({"vmid": data["newid"], "name": data["name"], "status": "stopped",
                             "tags": ""})
            # A clone inherits the source's disk; model one so sizing is exercised.
            self.configs[data["newid"]] = {
                "description": data.get("description") or "",
                "scsi0": f"hdd:vm-{data['newid']}-disk-0,size=10G",
            }
            return {"data": "UPID:clone"}
        if tail == "config":
            if method == "GET":
                return {"data": dict(self.configs.get(vmid, {}))}
            self.configs.setdefault(vmid, {}).update(data or {})
            return {"data": None}
        if tail == "resize":
            return {"data": None}
        if tail and tail.startswith("status/"):
            vm = next(v for v in self.vms if v["vmid"] == vmid)
            vm["status"] = "running" if tail.endswith("start") else "stopped"
            return {"data": "UPID:status"}
        if tail is None and method == "DELETE":
            self.vms[:] = [v for v in self.vms if v["vmid"] != vmid]
            self.configs.pop(vmid, None)
            return {"data": "UPID:destroy"}
        raise AssertionError(f"unexpected call {method} {path}")

    def config_write(self, vmid):
        """The config PUTs applied to vmid, merged in order."""
        merged = {}
        for method, path, data in self.calls:
            if method == "PUT" and path.endswith(f"/qemu/{vmid}/config"):
                merged.update(data or {})
        return merged


def target(identifier="120", box_name="web01", vmid=1401, ip="192.168.120.2", **box):
    spec = {"name": box_name, "cpu": 2, "memory_mb": 2048, "disk_gb": 20,
            "template": "base-debian13", "last_octet": 2}
    spec.update(box)
    return {"team_key": "team1", "identifier": identifier, "box": spec, "box_name": box_name,
            "box_idx": 0, "ip": ip, "vmid": vmid, "vm_name": f"team1-{box_name}", "node": NODE,
            "slot": 0}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = EstateState.load(Path(self.tmp.name))
        self.ownership = Ownership("scrim-one", "run-abc123")
        self.fake = FakePVE()
        # Each module binds proxmox_api into its own namespace, so patching infra.client
        # alone would leave estate.apply / infra.lifecycle / infra.ownership calling the
        # real API (docs/tests.md § Patching).
        for owner in (estate_apply, ownership_ops, lifecycle, client):
            patcher = mock.patch.object(owner, "proxmox_api", side_effect=self.fake)
            patcher.start()
            self.addCleanup(patcher.stop)


class Bridges(Base):
    def test_creates_an_isolated_bridge_and_records_it(self):
        created = estate_apply.create_bridge(self.state, NODE, "vmbr120", comment="team 120")
        self.assertTrue(created)
        post = [c for c in self.fake.calls if c[0] == "POST" and c[1].endswith("/network")]
        # No bridge_ports: PVE's parameter is the port LIST and it resolves every entry as
        # an existing interface, so "none" (the file spelling) 500s with "unable to find
        # bridge port 'none'" on real hardware caught live. An empty
        # list is expressed by omitting the key.
        self.assertNotIn("bridge_ports", post[0][2])
        self.assertEqual(post[0][2]["iface"], "vmbr120")
        # POST stages, PUT applies — a VM must not be attached to a staged bridge.
        self.assertIn(("PUT", f"/nodes/{NODE}/network", None), self.fake.calls)
        recorded = self.state.resources(kind="bridge")
        self.assertEqual([r["name"] for r in recorded], ["vmbr120"])

    def test_existing_bridge_is_left_alone(self):
        self.fake.networks.append({"iface": "vmbr120", "type": "bridge"})
        self.assertFalse(estate_apply.create_bridge(self.state, NODE, "vmbr120"))
        self.assertEqual(self.state.resources(kind="bridge"), [])


class Engine(Base):
    def _create(self, **kw):
        return estate_apply.create_engine(
            self.state, self.ownership, NODE, vmid=1300, template_vmid=1440,
            team_identifiers=["120", "121"], username="sysadmin",
            ssh_public_key="ssh-ed25519 AAAA", **kw)

    def test_clones_from_the_template_and_wires_every_team_bridge(self):
        self._create()
        clone = [c for c in self.fake.calls if c[1].endswith("/qemu/1440/clone")]
        self.assertEqual(clone[0][2], {"newid": 1300, "name": "quotient-engine", "full": 0,
                                       "description": "tezcatlipoca-clone comp-scrim-one"})
        config = self.fake.config_write(1300)
        self.assertEqual(config["net0"], "virtio,bridge=vmbr0")
        self.assertEqual(config["net1"], "virtio,bridge=vmbr120")
        self.assertEqual(config["net2"], "virtio,bridge=vmbr121")
        self.assertEqual(config["cores"], 4)
        self.assertEqual(config["memory"], 4096)
        self.assertIn("run-abc123", config["tags"])

    def test_the_management_nic_uses_the_resolved_bridge(self):
        # An estate whose management L2 is not vmbr0 (the cyber range's is vmbr100) used to
        # get VMs attached to vmbr0 — a bridge the node may not even have.
        self._create(mgmt_bridge="vmbr100")
        config = self.fake.config_write(1300)
        self.assertEqual(config["net0"], "virtio,bridge=vmbr100")
        self.assertEqual(config["net1"], "virtio,bridge=vmbr120")

    def test_transit_nics_come_after_every_team_nic(self):
        self._create(transit_identifiers=["120", "121"])
        config = self.fake.config_write(1300)
        self.assertEqual(config["net3"], "virtio,bridge=vmbrW120")
        self.assertEqual(config["net4"], "virtio,bridge=vmbrW121")

    def test_the_red_segment_nic_appends_after_the_transits(self):
        # The private red bridge is the range's own L2: without it red01's segment gateway
        # (10.200.0.1) is ambiguous with every other range's on the shared management L2.
        self._create(transit_identifiers=["120"], red_bridge="vmbrRabc12345")
        config = self.fake.config_write(1300)
        self.assertEqual(config["net4"], "virtio,bridge=vmbrRabc12345")

    def test_a_range_without_red_gets_no_red_nic(self):
        self._create()
        self.assertNotIn("net3", self.fake.config_write(1300))

    def test_static_mgmt_ip_gets_cloud_init(self):
        self._create(mgmt_ip="10.0.0.248", mgmt_gw="10.0.0.1")
        config = self.fake.config_write(1300)
        self.assertEqual(config["ipconfig0"], "ip=10.0.0.248/24,gw=10.0.0.1")
        self.assertEqual(config["ciuser"], "sysadmin")
        self.assertIn("sshkeys", config)

    def test_without_mgmt_ip_no_cloud_init_block(self):
        self._create()
        config = self.fake.config_write(1300)
        self.assertNotIn("ipconfig0", config)
        self.assertNotIn("ciuser", config)

    def test_records_the_engine_resource(self):
        self._create()
        recorded = self.state.resources(kind="engine")
        self.assertEqual([r["vmid"] for r in recorded], [1300])
        self.assertEqual(recorded[0]["node"], NODE)
        self.assertIn("run-abc123", recorded[0]["tags"])


class TeamBox(Base):
    def _create(self, t=None, source=1451):
        return estate_apply.create_team_box(
            self.state, self.ownership, t or target(), source_vmid=source,
            box_username="ubuntu", box_password="pw", ssh_public_key="ssh-ed25519 AAAA")

    def test_linux_box_gets_cloud_init_identity(self):
        self._create()
        config = self.fake.config_write(1401)
        self.assertEqual(config["net0"], "virtio,bridge=vmbr120")
        self.assertEqual(config["ciuser"], "ubuntu")
        self.assertEqual(config["cipassword"], "pw")
        self.assertEqual(config["ipconfig0"], "ip=192.168.120.2/24,gw=192.168.120.1")
        self.assertEqual(config["nameserver"], "8.8.8.8")
        self.assertEqual(config["cores"], 2)
        self.assertEqual(config["memory"], 2048)

    def test_in_path_firewall_wan_nic_is_first(self):
        self._create(target(box_name="fw01", in_path=True, unmanaged=True))
        config = self.fake.config_write(1401)
        self.assertEqual(config["net0"], "virtio,bridge=vmbrW120")
        self.assertEqual(config["net1"], "virtio,bridge=vmbr120")

    def test_unmanaged_appliance_gets_no_cloud_init(self):
        self._create(target(box_name="fw01", unmanaged=True))
        config = self.fake.config_write(1401)
        self.assertNotIn("ciuser", config)
        self.assertNotIn("ipconfig0", config)

    def test_windows_box_gets_no_cloud_init(self):
        self._create(target(box_name="dc01", template="base-windows-server"))
        config = self.fake.config_write(1401)
        self.assertNotIn("ciuser", config)
        self.assertNotIn("ipconfig0", config)

    def test_records_the_box(self):
        self._create()
        recorded = self.state.resources(kind="team_box")
        self.assertEqual([(r["name"], r["vmid"]) for r in recorded],
                         [("team1-web01", 1401)])


class Idempotency(Base):
    def test_existing_owned_vm_is_adopted_without_cloning(self):
        self.fake.vms.append({"vmid": 1401, "name": "team1-web01", "status": "stopped",
                              "tags": "tezcatlipoca;comp-scrim-one;run-abc123"})
        self.fake.configs[1401] = {"tags": "tezcatlipoca;comp-scrim-one;run-abc123"}
        estate_apply.create_team_box(self.state, self.ownership, target(), source_vmid=1451,
                                     box_username="ubuntu", box_password="pw",
                                     ssh_public_key="ssh-ed25519 AAAA")
        self.assertEqual([c for c in self.fake.calls if c[1].endswith("/clone")], [])
        self.assertEqual(self.state.resources(kind="team_box")[0]["vmid"], 1401)

    def test_foreign_vm_is_refused(self):
        self.fake.vms.append({"vmid": 1401, "name": "someone-else", "status": "running",
                              "tags": "cloud-init;template"})
        self.fake.configs[1401] = {"tags": "cloud-init;template"}
        with self.assertRaises(Exception) as raised:
            estate_apply.create_team_box(self.state, self.ownership, target(), source_vmid=1451,
                                         box_username="ubuntu", box_password="pw",
                                         ssh_public_key="ssh-ed25519 AAAA")
        self.assertIn("leftover range", str(raised.exception))
        self.assertEqual([c for c in self.fake.calls if c[1].endswith("/clone")], [])

    def test_interrupted_clone_is_rebuilt(self):
        self.fake.vms.append({"vmid": 1401, "name": "team1-web01", "status": "stopped",
                              "tags": ""})
        self.fake.configs[1401] = {"description": "tezcatlipoca-clone comp-scrim-one"}
        estate_apply.create_team_box(self.state, self.ownership, target(), source_vmid=1451,
                                     box_username="ubuntu", box_password="pw",
                                     ssh_public_key="ssh-ed25519 AAAA")
        self.assertIn(("DELETE", f"/nodes/{NODE}/qemu/1401", None), self.fake.calls)
        self.assertTrue([c for c in self.fake.calls if c[1].endswith("/qemu/1451/clone")])


class Disk(Base):
    def test_grows_only_upward(self):
        self.fake.configs[1401] = {"scsi0": "hdd:vm-1401-disk-0,size=10G"}
        estate_apply.ensure_disk_size(NODE, 1401, 20)
        resize = [c for c in self.fake.calls if c[1].endswith("/resize")]
        self.assertEqual(resize[0][2], {"disk": "scsi0", "size": "20G"})

    def test_never_shrinks(self):
        self.fake.configs[1401] = {"scsi0": "hdd:vm-1401-disk-0,size=40G"}
        estate_apply.ensure_disk_size(NODE, 1401, 20)
        self.assertEqual([c for c in self.fake.calls if c[1].endswith("/resize")], [])

    def test_discard_is_added_to_the_inherited_volume(self):
        self.fake.configs[1401] = {"scsi0": "hdd:vm-1401-disk-0,size=20G"}
        estate_apply.set_discard(NODE, 1401, "scsi0")
        self.assertEqual(self.fake.config_write(1401)["scsi0"],
                         "hdd:vm-1401-disk-0,discard=on")


if __name__ == "__main__":
    unittest.main()
