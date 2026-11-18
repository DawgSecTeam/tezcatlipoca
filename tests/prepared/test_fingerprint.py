"""The prepared-point fingerprint: parsers over transcripts, and the reset's claim measured.

The transcripts below are shaped like the real output of the probes (red01's split-brain boot is
the case that cost a round), so the parsers are pinned against what a reboot actually reports.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from prepared import fingerprint as fp  # noqa: E402

PORTS = {"daemon_port": "31337", "server": [5353, 8080, 8443, 8888, 51820]}

# What the build-only server winning the boot race looks like: mTLS owned by the :31338 daemon,
# dns and wg restored by neither.
SPLIT_BOOT = """\
tlaloc.service                           disabled enabled
sliver.service                           enabled  enabled
sliver-build-1.service                   disabled enabled
#active
inactive
#daemons
777 /opt/sliver/sliver-server daemon --lhost 10.0.0.245 --lport 31337
775 /opt/sliver/sliver-server daemon --lhost 10.0.0.245 --lport 31338
#listeners
tcp   LISTEN 0 4096  *:8080  *:*  users:(("sliver-server",pid=777,fd=12))
tcp   LISTEN 0 4096  *:8888  *:*  users:(("sliver-server",pid=775,fd=11))
tcp   LISTEN 0 4096  *:8443  *:*  users:(("sliver-server",pid=777,fd=13))
"""

CLEAN_BOOT = SPLIT_BOOT.replace("pid=775", "pid=777") + \
    "udp   UNCONN 0 0  *:5353  *:*  users:((\"sliver-server\",pid=777,fd=15))\n" \
    "udp   UNCONN 0 0  0.0.0.0:51820  0.0.0.0:*  users:((\"sliver-server\",pid=777,fd=14))\n"


def _spec(comp):
    return {"engine": (3300, "pve"),
            "boxes": [("team1-web01", 1721, "pve", "192.168.152.4", False),
                      ("team1-win02", 1720, "pve", "192.168.152.5", True)],
            "vms": [("team1-web01", 1721, "pve"), ("team1-win02", 1720, "pve"),
                    ("engine", 3300, "pve"), ("red01", 3460, "pve")],
            "red": {"ip": "10.0.0.245", "ports": PORTS, "ssh_key": "/k"}}


class Parsers(unittest.TestCase):
    def test_listening_ports_are_reduced_to_a_sorted_set(self):
        self.assertEqual(fp.parse_ports("22\n80\n443\n22\n"), [22, 80, 443])

    def test_the_dynamic_range_is_not_range_state(self):
        # Windows' RPC endpoint mapper rebinds these every boot; an implant's listener is not
        # in the range, so a survivor is still reported.
        self.assertEqual(fp.parse_ports("135\n49167\n8888\n"), [135, 8888])

    def test_ownership_is_the_daemon_not_the_pid(self):
        state = fp.parse_red_state(SPLIT_BOOT, PORTS)
        self.assertEqual(state["listeners"]["8888"], "build:31338")
        self.assertEqual(state["listeners"]["8080"], "main")
        self.assertEqual(state["listeners"]["5353"], "absent")
        self.assertFalse(state["armed"])

    def test_a_clean_boot_puts_every_transport_on_the_main_server(self):
        state = fp.parse_red_state(CLEAN_BOOT, PORTS)
        self.assertEqual(set(state["listeners"].values()), {"main"})
        # The STATE column, not the preset: an installed-and-held tlaloc reads disabled/enabled,
        # and the preset never moves.
        self.assertEqual(state["units"]["sliver.service"], "enabled")
        self.assertEqual(state["units"]["tlaloc.service"], "disabled")
        self.assertEqual(state["units"]["sliver-build-1.service"], "disabled")

    def test_inactive_is_not_active(self):
        for text, want in (("inactive\n", False), ("active\n", True)):
            raw = "\n".join(["tlaloc.service static disabled", "#active", text])
            self.assertIs(fp.parse_red_state(raw, PORTS)["armed"], want)

    def test_a_range_with_no_c2_has_no_daemon_to_own_anything(self):
        self.assertEqual(fp.parse_red_state(SPLIT_BOOT, None)["listeners"], {})

    def test_rules_keep_the_lines_that_describe_the_comp(self):
        rules = fp.parse_rules("-N RANGE-RED-DNAT\n"
                               "-A RANGE-RED-FWD -s 10.200.0.0/16 -j ACCEPT\n"
                               "-A PREROUTING -m comment --comment x -j RANGE-RED-DNAT\n"
                               "-P FORWARD ACCEPT\n")
        self.assertEqual(len(rules), 3)


class CaptureAndCompare(unittest.TestCase):
    def _capture(self, listeners="main"):
        raw = CLEAN_BOOT if listeners == "main" else SPLIT_BOOT

        def api(method, path, **kw):
            if path.endswith("/snapshots"):
                return {"data": [{"name": "tz-base"}, {"name": "tz-ready"}]}
            return {"data": {"net0": "virtio,bridge=vmbr1", "memory": 4096,
                              "digest": "volatile", "status": "running"}}

        def agent(node, vmid, script, **kw):
            return 0, ("22\n80\n" if "ss -H" in script else
                       "-N RANGE-RED-DNAT\n-A RANGE-RED-FWD -s 10.200.0.0/16 -j ACCEPT\n"), ""

        def windows(node, vmid, script, **kw):
            return 0, "21\n135\n", ""

        def ssh(key, ip, remote, **kw):
            return SimpleNamespace(returncode=0, stdout=raw, stderr="")

        with mock.patch.object(fp, "proxmox_api", api), \
                mock.patch.object(fp, "list_snapshots",
                                  lambda node, vmid: {"tz-base", "tz-ready"}), \
                mock.patch.object(fp, "guest_agent_exec_root", agent), \
                mock.patch.object(fp, "guest_agent_exec_windows", windows), \
                mock.patch("red.tlaloc_ops._ssh_run", ssh):
            return fp.capture(_spec("/c"))

    def test_the_same_reading_differs_from_nothing(self):
        before = self._capture()
        self.assertEqual(fp.compare(before, self._capture()), [])

    def test_a_transport_held_by_the_build_server_is_named(self):
        before = self._capture()
        now = self._capture(listeners="build")
        deltas = fp.compare(before, now)
        self.assertTrue(any("red01.listeners.8888" in d for d in deltas), deltas)

    def test_a_dead_box_is_a_finding_not_a_crash(self):
        with mock.patch.object(fp, "proxmox_api",
                               lambda m, p, **k: {"data": {}}), \
                mock.patch.object(fp, "list_snapshots", lambda node, vmid: {"tz-ready"}), \
                mock.patch.object(fp, "guest_agent_exec_root",
                                  mock.Mock(side_effect=RuntimeError("no agent"))), \
                mock.patch.object(fp, "guest_agent_exec_windows",
                                  mock.Mock(side_effect=RuntimeError("no agent"))), \
                mock.patch("red.tlaloc_ops._ssh_run",
                           lambda k, i, r, **kw: SimpleNamespace(returncode=0,
                                                                 stdout=CLEAN_BOOT, stderr="")):
            state = fp.capture(_spec("/c"))
        self.assertTrue(state["boxes"]["team1-web01"].startswith("unreachable:"))

    def test_a_reference_filed_with_vmgenid_still_compares_clean(self):
        ref = {"probe": fp.PROBE,
               "vms": {"engine": {"config": {"cores": 2, "vmid": 3300,
                                             "vmgenid": "old-uuid"},
                                  "snapshots": ["tz-ready"]}},
               "boxes": {}, "engine": {"rules": []}, "red01": {}}
        now = {"vms": {"engine": {"config": {"cores": 2, "vmid": 3300},
                                  "snapshots": ["tz-ready"]}},
               "boxes": {}, "engine": {"rules": []}, "red01": {}}
        self.assertEqual(fp.compare(ref, now), [])

    def test_a_vm_definition_keeps_only_what_the_range_is(self):
        # `vmgenid` moves on every start by design, so a reset's reboot changes it — reporting
        # it as a range that moved is how the first reset of a clean range refused.
        cfg = {"name": "c", "vmid": 1721, "cores": 2, "net0": "virtio,bridge=vmbr152",
               "vmgenid": "11111111-2222-3333-4444-555555555555", "digest": "abc",
               "lock": None, "template": 0}
        with mock.patch.object(fp, "proxmox_api", lambda *a, **k: {"data": cfg}), \
                mock.patch.object(fp, "list_snapshots", lambda n, v: {"tz-ready"}):
            out = fp._vms({"vms": [("team1-web01", 1721, "pve")]})
        self.assertEqual(out["team1-web01"]["config"],
                         {"cores": 2, "net0": "virtio,bridge=vmbr152", "vmid": 1721})

    def test_the_engine_probe_asks_each_chain_in_its_own_table(self):
        # `iptables -S RANGE-RED-DNAT` without `-t nat` answers "no chain": the nat rules read
        # as absent while installed, which is how a reset would refuse a clean range.
        self.assertIn("iptables -t nat -S RANGE-RED-DNAT", fp._ENGINE_RULES)
        self.assertIn("iptables -t nat -S RANGE-RED-SNAT", fp._ENGINE_RULES)
        self.assertIn("iptables -t filter -S RANGE-RED-FWD", fp._ENGINE_RULES)
        self.assertNotIn("iptables -S RANGE-RED-DNAT", fp._ENGINE_RULES)

    def test_check_waits_for_the_engines_timer_to_reassert_the_rules(self):
        # Kernel chains are rebuilt by the on-disk timer 35 s after the reset's boot: a read
        # taken before it fires is not a delta, it is early.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rules = {"RANGE-RED-DNAT": True}
        seen = []

        def _engine(s):
            seen.append(1)
            return {"rules": ["-N RANGE-RED-DNAT"] if len(seen) > 1 else []}

        ref = {"engine": {"rules": ["-N RANGE-RED-DNAT"]}, "vms": {}, "boxes": {},
               "red01": {}, "probe": fp.PROBE}
        (Path(tmp.name) / fp.REFERENCE).write_text(json.dumps(ref))
        with mock.patch.object(fp, "spec", lambda comp_dir: _spec(comp_dir)), \
                mock.patch.object(fp, "_engine", _engine), \
                mock.patch.object(fp, "_boxes", lambda s: {}), \
                mock.patch.object(fp, "_vms", lambda s: {}), \
                mock.patch.object(fp, "_red01", lambda s: {}), \
                mock.patch.object(fp.time, "sleep", lambda _s: None):
            self.assertEqual(fp.check(tmp.name), [])
        self.assertGreater(len(seen), 1, "the engine must be re-read, not judged on one look")

    def test_a_reference_from_an_older_probe_is_refiled_not_compared(self):
        # Probe 1 asked for the DNAT chain in the filter table, so its reference has no nat
        # rules. That is the instrument growing, not the range moving.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ref = {"vms": {}, "boxes": {}, "engine": {"rules": []}, "red01": {}, "probe": 1}
        (Path(tmp.name) / fp.REFERENCE).write_text(json.dumps(ref))
        with mock.patch.object(fp, "spec", lambda comp_dir: _spec(comp_dir)), \
                mock.patch.object(fp, "_engine", lambda s: {"rules": ["-N RANGE-RED-DNAT"]}), \
                mock.patch.object(fp, "_boxes", lambda s: {}), \
                mock.patch.object(fp, "_vms", lambda s: {}), \
                mock.patch.object(fp, "_red01", lambda s: {}):
            deltas = fp.check(tmp.name)
            self.assertEqual(len(deltas), 1, deltas)
            self.assertTrue(deltas[0].startswith("note: the engine-rules probe grew"), deltas)
            self.assertEqual(json.loads((Path(tmp.name) / fp.REFERENCE).read_text())
                             ["engine"]["rules"], ["-N RANGE-RED-DNAT"])
            self.assertEqual(fp.check(tmp.name), [])

    def test_an_old_probe_still_measures_every_other_class(self):
        # Only the rules class is re-recorded: a box that came back with another port set is
        # still a delta, however old the reference is.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ref = {"vms": {}, "boxes": {"team1-web01": [22, 80]}, "engine": {"rules": []},
               "red01": {}, "probe": 1}
        (Path(tmp.name) / fp.REFERENCE).write_text(json.dumps(ref))
        with mock.patch.object(fp, "spec", lambda comp_dir: _spec(comp_dir)), \
                mock.patch.object(fp, "_engine", lambda s: {"rules": []}), \
                mock.patch.object(fp, "_boxes", lambda s: {"team1-web01": [22, 80, 8888]}), \
                mock.patch.object(fp, "_vms", lambda s: {}), \
                mock.patch.object(fp, "_red01", lambda s: {}):
            deltas = fp.check(tmp.name)
        self.assertEqual(len(deltas), 2, deltas)
        self.assertTrue(deltas[0].startswith("note: the engine-rules probe grew"), deltas)
        self.assertIn("team1-web01", deltas[1], deltas)

    def test_check_records_a_range_that_has_no_reference_yet(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(fp, "spec", lambda comp_dir: _spec(comp_dir)), \
                mock.patch.object(fp, "capture", lambda s: {"vms": {}}):
            deltas = fp.check(tmp.name)
            self.assertTrue(deltas[0].startswith("note: no reference"), deltas)
            self.assertEqual(json.loads((Path(tmp.name) / fp.REFERENCE).read_text()),
                             {"vms": {}, "probe": fp.PROBE})
            self.assertEqual(fp.check(tmp.name), [])



class Spec(unittest.TestCase):
    """The inventory is read out of the range's own state document, in the shape the deploy
    writes — a spec built from the wrong section silently fingerprints nothing."""

    def test_the_flat_view_names_every_vm_class(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        comp = Path(tmp.name)
        (comp / "estate-state.json").write_text(json.dumps({
            "schema_version": 2,
            "identity": {"comp_name": "c", "run_id": "run-1", "node": "proxmox",
                         "scoring_vm_id": 3300},
            "roster": {"box_order": ["web01", "win02"], "targets": {
                "152-web01": {"team_key": "team1", "box_name": "web01", "identifier": "152",
                              "vmid": 1721, "ip": "192.168.152.4", "node": "proxmox", "slot": 0},
                "153-win02": {"team_key": "team2", "box_name": "win02", "identifier": "153",
                              "vmid": 1730, "ip": "192.168.153.5", "node": "proxmox",
                              "slot": 1}}},
            "run": {"red": {"vmid": 3460, "node": "proxmox", "red_ip": "10.0.0.245"}}}))
        (comp / "boxes.json").write_text(json.dumps(
            [{"name": "web01", "template": "tez-debian13-web01"},
             {"name": "win02", "template": "tez-win11-win02"}]))
        s = fp.spec(comp)
        self.assertEqual(s["engine"], (3300, "proxmox"))
        self.assertEqual(s["boxes"], [("team1-web01", 1721, "proxmox", "192.168.152.4", False),
                                      ("team2-win02", 1730, "proxmox", "192.168.153.5", True)])
        self.assertEqual([v[0] for v in s["vms"]],
                         ["team1-web01", "team2-win02", "engine", "red01"])
        self.assertEqual(s["red"]["ip"], "10.0.0.245")
        self.assertEqual(s["red"]["ports"]["daemon_port"], "31337")
        # The probes consume this spec, so the fake above is not the only shape they must take:
        # a capture over the REAL spec is what pins the box tuple's arity (a deploy died on it).
        with mock.patch.object(fp, "_agent_exec", lambda *a, **k: "22\n80\n"), \
                mock.patch.object(fp, "_vms", lambda spec: {}), \
                mock.patch.object(fp, "_red01", lambda spec: {"units": {}}):
            cap = fp.capture(s)
        self.assertEqual(cap["boxes"], {"team1-web01": [22, 80],
                                        "team2-win02": [22, 80]})


if __name__ == "__main__":
    unittest.main()
