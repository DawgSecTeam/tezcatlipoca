"""build_jump_vms runs satellites concurrently instead of one host at a time.

Why this test exists: the serial loop was measured at 17s..547s per satellite
(avg ~150s, n=17) in multinode-spread-2026-09-30, so an 8-satellite spread paid
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

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import jump_ops  # noqa: E402
from nodes_ops import NodeRecord  # noqa: E402


def _placement(n_sats, teams_per_sat=2):
    sats, nodes, team_nodes, team_ids, slots = [], {}, {}, {}, {}
    for i in range(n_sats):
        name = f"sat{i + 1}"
        nodes[name] = NodeRecord(
            name=name, endpoint=f"https://10.0.0.{160 + i}:8006", node=f"n{i + 1}",
            datastore="hdd", token_env="TF_VAR_proxmox_api_token",
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
                           run_id=None, red_segment=""):
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

    def test_red_segment_reaches_every_satellite_build(self):
        """The segment must survive the thread-pool hop, not just the signature.

        scale8 soak 2026-10-02: routed red could not reach any satellite team because
        the jump's FORWARD policy dropped its source. The fix is only as good as this
        plumbing — a segment that stops at build_jump_vms leaves the range unreachable.
        """
        placement = _placement(3)
        seen = []
        lock = threading.Lock()

        def fake_build_one(rec, sat, vmid, team_ids, ctx, comp_name, eng_ip, eng_gw,
                           run_id=None, red_segment=""):
            with lock:
                seen.append(red_segment)

        with patch.object(jump_ops, "_build_one", side_effect=fake_build_one):
            jump_ops.build_jump_vms(placement, engine_vmid=1000, ctx={}, comp_name="t",
                                    engine_mgmt_ip="10.0.0.250",
                                    red_segment="10.200.0.0/24")

        self.assertEqual(seen, ["10.200.0.0/24"] * 3)

    def test_no_red_segment_is_passed_through_as_empty(self):
        """Absent knob must reach the builder as "" so red-less ranges stay unchanged."""
        placement = _placement(2)
        seen = []

        with patch.object(jump_ops, "_build_one",
                          side_effect=lambda *a, **kw: seen.append(kw.get("red_segment"))):
            jump_ops.build_jump_vms(placement, engine_vmid=1000, ctx={}, comp_name="t",
                                    engine_mgmt_ip="10.0.0.250")

        self.assertEqual(seen, ["", ""])

    def test_builds_actually_overlap(self):
        """A serialised implementation would fail this: 4 units x 0.25s = 1.0s."""
        placement = _placement(4)

        def slow_build_one(*a, **kw):
            time.sleep(0.25)

        with patch.object(jump_ops, "_build_one", side_effect=slow_build_one):
            started = time.monotonic()
            jump_ops.build_jump_vms(placement, engine_vmid=1000, ctx={},
                                    comp_name="t", engine_mgmt_ip="10.0.0.250")
            elapsed = time.monotonic() - started

        # Bounded pool of 4 over 4 units: expect ~0.25s, not ~1.0s. Generous margin
        # for a loaded CI box, but still far below the serial floor.
        self.assertLess(elapsed, 0.75,
                        f"satellites did not overlap ({elapsed:.2f}s for 4x0.25s of work)")

    def test_pool_is_bounded_not_unbounded(self):
        """20 satellites must not open 20 concurrent clones onto shared datastores."""
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

    def test_a_failing_satellite_aborts_the_deploy(self):
        """The serial loop raised on the first failure; that must be preserved."""
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


if __name__ == "__main__":
    unittest.main()
