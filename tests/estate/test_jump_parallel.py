"""build_jump_vms runs satellites concurrently instead of one host at a time.

Why this test exists: the serial loop was measured at 17s..547s per satellite
(avg ~150s, n=17) measured on a spread multinode run, so an 8-satellite spread paid
~18 minutes of pure waiting. Satellites are independent Proxmox hosts reached over
the direct API path and the mgmt LAN, so the wait collapses under a bounded pool.

The regression risk is not "did it get faster" but "did it stay correct": a
concurrent rewrite can silently drop satellites, swallow a failure that the serial
loop would have propagated, or turn a hard abort into a partial build. Those three
properties are what these tests pin.
"""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from net import jump_ops  # noqa: E402
from multinode.nodes_config import NodeRecord  # noqa: E402


def _placement(n_sats, teams_per_sat=2):
    sats, nodes, team_nodes, team_ids, slots = [], {}, {}, {}, {}
    for i in range(n_sats):
        name = f"sat{i + 1}"
        nodes[name] = NodeRecord(
            name=name, endpoint=f"https://10.0.0.{160 + i}:8006", node=f"n{i + 1}",
            datastore="hdd", token_env="TEZ_PROXMOX_API_TOKEN",
            jump_mgmt_ip=f"10.0.0.{249 - i}",
        ).to_json()
        teams = [f"team{i * teams_per_sat + j + 1}" for j in range(teams_per_sat)]
        for j, t in enumerate(teams):
            team_nodes[t] = name
            team_ids[t] = str(201 + i * teams_per_sat + j)
            slots[t] = i + 1
        sats.append({
            "name": name, "slot": i + 1, "jump_vmid": 1900 + i,
            "jump_mgmt_ip": f"10.0.0.{249 - i}", "teams": teams,
        })
    return {
        "satellites": sats, "nodes": nodes, "team_nodes": team_nodes,
        "team_identifiers": team_ids, "team_slots": slots,
        "engine_node": "sat1",
    }


class BuildJumpVmsConcurrency(unittest.TestCase):
    def test_every_satellite_is_built_and_returned(self):
        placement = _placement(3)
        seen = []
        lock = threading.Lock()

        def fake_build_one(rec, sat, vmid, team_ids, ctx, comp_name, eng_ip, eng_gw,
                           run_id=None, red_segment="", mgmt_bridge="vmbr0"):
            with lock:
                seen.append((sat["name"], vmid, tuple(team_ids)))

        with patch.object(jump_ops, "_build_one", side_effect=fake_build_one):
            result = jump_ops.build_jump_vms(
                placement, engine_vmid=1000, ctx={}, comp_name="t",
                engine_mgmt_ip="10.0.0.250")

        self.assertEqual(result, {"sat1": 1900, "sat2": 1901, "sat3": 1902})
        self.assertEqual(len(seen), 3, "concurrent rewrite must not drop a satellite")
        # team_ids must still be that satellite's own teams, sorted as ints
        by_name = {n: t for n, _, t in seen}
        self.assertEqual(by_name["sat1"], ("201", "202"))
        self.assertEqual(by_name["sat3"], ("205", "206"))

    # a scale soak found routed red could not reach any satellite team because the
    # jump's FORWARD policy dropped its source — a segment that stops at build_jump_vms
    # leaves the range unreachable; it must survive the thread-pool hop.
    def test_red_segment_reaches_every_satellite_build(self):
        placement = _placement(3)
        seen = []
        lock = threading.Lock()

        def fake_build_one(rec, sat, vmid, team_ids, ctx, comp_name, eng_ip, eng_gw,
                           run_id=None, red_segment="", mgmt_bridge="vmbr0"):
            with lock:
                seen.append(red_segment)

        with patch.object(jump_ops, "_build_one", side_effect=fake_build_one):
            jump_ops.build_jump_vms(placement, engine_vmid=1000, ctx={}, comp_name="t",
                                    engine_mgmt_ip="10.0.0.250",
                                    red_segment="10.200.0.0/24")

        self.assertEqual(seen, ["10.200.0.0/24"] * 3)

    # 20 satellites must not open 20 concurrent clones onto shared datastores
    def test_pool_is_bounded_not_unbounded(self):
        placement = _placement(20)
        state = {"cur": 0, "peak": 0}
        lock = threading.Lock()

        def counting_build_one(*a, **kw):
            with lock:
                state["cur"] += 1
                state["peak"] = max(state["peak"], state["cur"])
            time.sleep(0.05)
            with lock:
                state["cur"] -= 1

        with patch.object(jump_ops, "_build_one", side_effect=counting_build_one):
            jump_ops.build_jump_vms(placement, engine_vmid=1000, ctx={},
                                    comp_name="t", engine_mgmt_ip="10.0.0.250")

        self.assertLessEqual(state["peak"], 4, "worker pool exceeded its bound")
        self.assertGreater(state["peak"], 1, "work was not actually concurrent")

    # the serial loop raised on the first failure; that must be preserved
    def test_a_failing_satellite_aborts_the_deploy(self):
        placement = _placement(3)

        def explode_on_sat2(rec, sat, *a, **kw):
            if sat["name"] == "sat2":
                raise RuntimeError("jump never came up on SSH")

        with patch.object(jump_ops, "_build_one", side_effect=explode_on_sat2):
            with self.assertRaises(RuntimeError) as cm:
                jump_ops.build_jump_vms(placement, engine_vmid=1000, ctx={},
                                        comp_name="t", engine_mgmt_ip="10.0.0.250")
        self.assertIn("never came up", str(cm.exception))

    def test_no_satellites_is_a_noop(self):
        with patch.object(jump_ops, "_build_one") as build:
            self.assertEqual(
                jump_ops.build_jump_vms({"satellites": []}, 1000, {}, "t", "10.0.0.250"),
                {})
            build.assert_not_called()


class JumpManagementBridge(unittest.TestCase):
    def _net0(self, rec_bridge, placement_bridge):
        placement = _placement(1)
        placement["mgmt_bridge"] = placement_bridge or "vmbr0"
        rec = NodeRecord.from_json({**placement["nodes"]["sat1"],
                                    "mgmt_bridge": rec_bridge})
        writes = []

        def api(method, path, **kw):
            if method == "GET":
                raise RuntimeError("no such VM")
            writes.append(kw.get("data"))
            return {"data": None}

        with patch.object(jump_ops, "proxmox_api", side_effect=api), \
                patch.object(jump_ops, "find_jump_template", return_value={"alpine": 900}), \
                patch.object(jump_ops, "destroy_vm_if_exists"), \
                patch.object(jump_ops, "gc_orphan_volumes"), \
                patch.object(jump_ops, "wait_for_proxmox_task"), \
                patch.object(jump_ops, "start_vm", side_effect=RuntimeError("built")), \
                patch.object(jump_ops, "vm_status", return_value="stopped"):
            with self.assertRaises(RuntimeError):
                jump_ops._build_one(rec, placement["satellites"][0], 1900, ["201"],
                                    {"ssh_public_key_quoted": "ssh-ed25519 AAAA"},
                                    "t", "10.0.0.250", "10.0.0.1",
                                    mgmt_bridge=placement["mgmt_bridge"])
        return next(d["net0"] for d in writes if d and "net0" in d)

    def test_the_node_records_own_bridge_wins(self):
        self.assertEqual(self._net0("vmbr100", "vmbr0"), "virtio,bridge=vmbr100")

    def test_the_placements_resolved_bridge_is_the_fallback(self):
        self.assertEqual(self._net0("", "vmbr100"), "virtio,bridge=vmbr100")


if __name__ == "__main__":
    unittest.main()
