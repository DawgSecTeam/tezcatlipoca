"""red01: the red VM is a tezcatlipoca-owned range component.

Identity is reserved per competition, the network is pinned (both NICs + the team
route), the engine's red-segment rules are ours, and destroy is bound to the
run's ownership tags.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from red import red01_ops as r  # noqa: E402


def _comp(tmp, body=""):
    d = Path(tmp) / "comp"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Compfile").write_text("name demo\n" + body)
    return d


class Identity(unittest.TestCase):
    def test_the_stride_is_one_band_past_the_last_slot(self):
        self.assertEqual(r.default_red_vmid(1300, 0), 1460)
        self.assertEqual(r.default_red_vmid(1300, 2), 1480)

    def test_the_compfile_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp, "red01_vmid 1777\nred01_ip 10.0.0.241\n")
            self.assertEqual(r.red01_vmid_for(comp, 1300), 1777)
            self.assertEqual(r.red01_ip_for(comp), "10.0.0.241")
            self.assertTrue(r.red01_ip_is_explicit(comp))

    def test_the_recorded_placement_wins_over_the_stride(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp)
            placement = {"red01": {"vmid": 1666, "ip": "10.0.0.245"}}
            self.assertEqual(r.red01_vmid_for(comp, 1300, placement), 1666)
            self.assertEqual(r.red01_ip_for(comp, placement), "10.0.0.245")

    def test_no_resolution_means_no_vmid(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(r.red01_vmid_for(_comp(tmp), None))

    def test_identity_reports_the_slot_and_explicit_ip(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp, "red01_ip 10.0.0.240\n")
            ident = r.identity(comp, 1300, "pve")
        self.assertEqual(ident["vmid"], 1460)
        self.assertEqual(ident["node"], "pve")
        self.assertTrue(ident["explicit_ip"])


class Network(unittest.TestCase):
    def test_the_netplan_pins_both_nics_and_the_team_route(self):
        text = r._netplan("ens18", "10.0.0.241", "10.0.0.1", "ens19")
        self.assertIn("addresses: ['10.0.0.241/24']", text)
        self.assertIn("via: 10.0.0.1", text)
        self.assertIn("addresses: ['10.200.0.10/24']", text)
        self.assertIn("to: 192.168.0.0/16\n          via: 10.200.0.1", text)
        self.assertIn("nameservers:\n        addresses: [8.8.8.8]", text)


class Rules(unittest.TestCase):
    def _script(self, **kw):
        args = dict(team_identifiers=[101, 102], callback_port=80,
                    callback_host="172.31.101.1")
        args.update(kw)
        return r.firewall_script(**args)

    def test_the_forward_trio_is_ordered(self):
        s = self._script()
        self.assertLess(s.index("-s 10.200.0.0/24 -d 192.168.0.0/16 -j ACCEPT"),
                        s.index("--ctstate RELATED,ESTABLISHED"))
        self.assertLess(s.index("--ctstate RELATED,ESTABLISHED"),
                        s.index("-d 10.200.0.0/24 -j DROP"))

    def test_every_gateway_and_the_callback_host_are_dnat_targets(self):
        s = self._script()
        for dst in ("192.168.101.1", "192.168.102.1", "172.31.101.1"):
            self.assertIn(f"-d {dst} --dport 80", s)
        self.assertIn("--to-destination 10.200.0.10:80", s)
        self.assertIn("PREROUTING -p tcp", s)
        self.assertIn("PREROUTING -p udp", s)
        self.assertIn("-I PREROUTING 1", s)
        self.assertNotIn("-A PREROUTING", s)

    def test_the_c2_flow_is_exempted_from_the_team_snat(self):
        s = self._script()
        self.assertIn("-t nat -I POSTROUTING 1 -s 192.168.0.0/16 -d 10.200.0.10", s)

    def test_the_c2_dnat_is_inserted_before_dockers_local_addrtype_jump(self):
        # Appending put it after `--dst-type LOCAL -j DOCKER`; the engine's own
        # published :80 answered the team box with a 404 and the DNAT counter
        # stayed at 0 (live run-b0bd41fe).
        s = self._script()
        self.assertIn("-t nat -D PREROUTING -p tcp", s)
        self.assertIn("-t nat -I PREROUTING 1 -p tcp", s)
        self.assertIn("-t nat -D PREROUTING -p udp", s)
        self.assertIn("-t nat -I PREROUTING 1 -p udp", s)
        self.assertNotIn("-t nat -A PREROUTING -p", s)

    def test_without_a_port_only_the_segment_rules_exist(self):
        s = self._script(callback_port=None)
        self.assertNotIn("DNAT", s)
        self.assertIn("DROP", s)

    def test_the_units_are_ours_and_separate_from_range_firewall(self):
        self.assertIn("range-red-firewall.sh", r._RED_SERVICE)
        self.assertNotIn("range-firewall.sh", r._RED_SERVICE.replace("range-red-firewall.sh", ""))
        self.assertIn("OnUnitActiveSec=30", r._RED_TIMER)


class Deploy(unittest.TestCase):
    def test_a_foreign_occupant_is_refused_not_reconfigured(self):
        from infra.ownership import Ownership
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp, "red01_vmid 1777\nred01_ip 10.0.0.241\n")
            key = Path(tmp) / "key"
            key.write_text("k")
            (Path(tmp) / "key.pub").write_text("ssh-rsa AAAA test")
            vms = [{"vmid": 1777, "node": "pve", "name": "someone-else",
                    "status": "running"}]
            with mock.patch.object(r, "proxmox_api") as api:
                api.side_effect = lambda method, path, **kw: (
                    {"data": vms} if path.endswith("/qemu") else
                    {"data": {"tags": "tezcatlipoca;comp-other;run-x"}})
                with self.assertRaises(RuntimeError) as cm:
                    r.deploy(comp, engine_vmid=1300, ssh_key=str(key),
                             ownership=Ownership("demo", "run-test"))
        self.assertIn("not this run's", str(cm.exception))

    def test_the_identity_is_recorded_without_a_placement_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp)
            r.record_identity(comp, None, vmid=1777, ip="10.0.0.241", node="pve")
            self.assertEqual(r.recorded_identity(comp),
                             {"vmid": 1777, "ip": "10.0.0.241", "node": "pve"})

    def test_the_mgmt_nic_is_matched_by_mac_not_the_default_route(self):
        from infra.ownership import Ownership
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp, "red01_vmid 1777\nred01_ip 10.0.0.241\n")
            key = Path(tmp) / "key"
            key.write_text("k")
            (Path(tmp) / "key.pub").write_text("ssh-rsa AAAA test")
            written = {}

            def fake_exec(node, vmid, script, timeout=300):
                if script.startswith("ls /sys/class/net/"):
                    return "br-abc123\ndocker0\nens18\nens19\n"
                if script.startswith("cat /sys/class/net/"):
                    return ("bc:24:11:aa:bb:cc\n" if "ens18" in script
                            else "bc:24:11:dd:ee:ff\n")
                if "route show default" in script:
                    return "default via 10.0.0.1 dev br-abc123\n"
                return ""

            with mock.patch.object(r, "_live", return_value={"vmid": 1777}), \
                    mock.patch.object(r, "_config"), \
                    mock.patch.object(r, "start_vm"), \
                    mock.patch.object(r, "wait_for_guest_agent", return_value=True), \
                    mock.patch.object(r, "_exec", side_effect=fake_exec), \
                    mock.patch.object(r, "_write",
                                      side_effect=lambda n, v, p, c, m: written.__setitem__(p, c)), \
                    mock.patch.object(r, "proxmox_api",
                                      return_value={"data": {
                                          "net0": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0"}}):
                r.deploy(comp, engine_vmid=1300, ssh_key=str(key),
                         ownership=Ownership("demo", "run-test"), node="pve")
        netplan = written["/etc/netplan/51-tlaloc.yaml"]
        self.assertIn("ens18:", netplan)
        self.assertIn("addresses: ['10.0.0.241/24']", netplan)
        self.assertIn("ens19:", netplan)
        self.assertNotIn("br-abc123", netplan)

    def test_a_missing_ssh_key_fails_before_any_clone(self):
        from infra.ownership import Ownership
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp, "red01_vmid 1777\nred01_ip 10.0.0.241\n")
            with mock.patch.object(r, "proxmox_api") as api:
                with self.assertRaises(RuntimeError) as cm:
                    r.deploy(comp, engine_vmid=1300, ssh_key="/tmp/nokey",
                             ownership=Ownership("demo", "run-test"))
            api.assert_not_called()
        self.assertIn("SSH public key", str(cm.exception))

    def test_the_cloud_init_wait_reattaches_after_an_agent_restart(self):
        from infra.ownership import Ownership
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp, "red01_vmid 1777\nred01_ip 10.0.0.241\n")
            key = Path(tmp) / "key"
            key.write_text("k")
            (Path(tmp) / "key.pub").write_text("ssh-rsa AAAA test")
            waits = []

            def fake_exec(node, vmid, script, timeout=300):
                if "cloud-init status --wait" in script:
                    waits.append(script)
                    if len(waits) == 1:
                        raise RuntimeError("guest agent on vmid 1777 lost exec pid 9 "
                                           "(the agent restarted mid-command)")
                if script.startswith("ls /sys/class/net/"):
                    return "eth0\nens19\n"
                if "route show default" in script:
                    return "default via 10.0.0.1 dev eth0\n"
                return ""

            with mock.patch.object(r, "_live", return_value={"vmid": 1777}), \
                    mock.patch.object(r, "_config"), \
                    mock.patch.object(r, "start_vm"), \
                    mock.patch.object(r, "wait_for_guest_agent", return_value=True) as wait, \
                    mock.patch.object(r, "_exec", side_effect=fake_exec), \
                    mock.patch.object(r, "_write"), \
                    mock.patch.object(r, "proxmox_api"):
                identity = r.deploy(comp, engine_vmid=1300, ssh_key=str(key),
                                    ownership=Ownership("demo", "run-test"), node="pve")
        self.assertEqual(identity["vmid"], 1777)
        self.assertEqual(len(waits), 2)
        self.assertEqual(wait.call_count, 2)


class Destroy(unittest.TestCase):
    def test_destroy_is_bound_to_the_run_tags(self):
        from infra.ownership import Ownership
        ownership = Ownership("demo", "run-test")
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp)
            with mock.patch.object(r, "recorded_identity",
                                   return_value={"vmid": 1777, "ip": "", "node": "pve"}), \
                    mock.patch.object(r, "destroy_vm_if_exists") as destroy, \
                    mock.patch.object(r, "remove_engine_rules"):
                r.destroy(comp, ownership=ownership)
        destroy.assert_called_once_with("pve", 1777, expect_tags=ownership.tags)

    def test_an_unrecorded_vmid_fails_loudly(self):
        from infra.ownership import Ownership
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp)
            with mock.patch.object(r, "recorded_identity",
                                   return_value={"vmid": None, "ip": "", "node": "pve"}):
                with self.assertRaises(RuntimeError) as cm:
                    r.destroy(comp, ownership=Ownership("demo", "run-test"))
        self.assertIn("not recorded", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
