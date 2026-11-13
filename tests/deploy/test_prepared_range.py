"""The prepared-range seam: a range is built and armed one stage, then reset whole.

Two properties are load-bearing and cheap to break:
  * no implant is on a box at the prepared restore point (`deploy_implants_now=False`),
  * a reset is range-atomic — boxes, red01 and the engine share one restored moment,
  * the event start arms the range before the clock, never after.
"""

import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from quotient import lifecycle_ops  # noqa: E402
from redeploy import redeploy_reset_ops as ro  # noqa: E402
from redeploy.redeploy_context import RedeployContext  # noqa: E402

BOX = {"team_key": "team1", "vm_name": "team1-web01", "vmid": 2001, "node": "pve",
       "box": {"name": "web01"}}


def _rctx(comp, state, teams=None, boxes=None):
    return RedeployContext(comp_dir=comp, name="c", comp_name="c", node="pve",
                           teams=teams if teams is not None else {"team1": {}},
                           boxes=boxes if boxes is not None else [{"name": "web01"}],
                           difficulty=None, state=state, state_path=None,
                           ctx={"scoring_engine_ip": "10.0.0.9", "ssh_key_path": "/k"})


class PreparedReset(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.comp = Path(self.tmp.name)

    def test_it_rolls_every_vm_class_back_to_one_moment(self):
        state = {"scoring_vm_id": 1000, "node": "pve",
                 "red": {"red_ip": "10.0.0.241", "vmid": 1690, "node": "pve"},
                 "injects_created": True, "injects_fingerprint": "f",
                 "engine_unpaused": True, "started": True, "red_deployed": True}
        rctx = _rctx(self.comp, state)
        with mock.patch.object(ro, "list_snapshots", return_value={ro.SNAP_READY}), \
                mock.patch.object(ro, "rollback_snapshot") as rollback, \
                mock.patch.object(ro.pipeline_api, "wait_for_boxes_ssh"), \
                mock.patch.object(ro, "wait_for_http"), \
                mock.patch.object(ro.estate_state, "write_flat"):
            done, _levels = ro.mode_prepared([BOX], rctx)
        self.assertEqual(sorted(c.args[1] for c in rollback.call_args_list),
                         [1000, 1690, 2001])
        self.assertEqual(done, [BOX])
        # The rollback took the inject windows with it: start must recreate them at now.
        self.assertNotIn("injects_created", state)
        self.assertNotIn("injects_fingerprint", state)
        self.assertFalse(state["engine_unpaused"])
        self.assertTrue(state["red_staged"])
        self.assertNotIn("red_deployed", state)

    def test_a_partial_selection_is_refused(self):
        rctx = _rctx(self.comp, {}, teams={"team1": {}, "team2": {}})
        with mock.patch.object(ro, "list_snapshots", return_value={ro.SNAP_READY}):
            with self.assertRaises(SystemExit) as cm:
                ro.mode_prepared([BOX], rctx)
        self.assertIn("WHOLE range", str(cm.exception))

    def test_a_range_without_prepared_points_is_refused_by_name(self):
        rctx = _rctx(self.comp, {})
        with mock.patch.object(ro, "list_snapshots", return_value=set()):
            with self.assertRaises(SystemExit) as cm:
                ro.mode_prepared([BOX], rctx)
        self.assertIn("team1-web01", str(cm.exception))


class StartArmsTheRange(unittest.TestCase):
    def _start(self, state, order):
        args = SimpleNamespace(engine_ip="10.0.0.9", scoring_password=None)

        class Session:
            def post(self, url, json=None, timeout=None):
                order.append("unpause" if "pause" in url else "clock")
                return SimpleNamespace(status_code=200,
                                       raise_for_status=lambda: None)

        with mock.patch.object(lifecycle_ops, "load_state", return_value=state), \
                mock.patch.object(lifecycle_ops, "login", return_value=Session()), \
                mock.patch.object(lifecycle_ops, "engine_status",
                                  return_value={"competition_started": False,
                                                "running": False}), \
                mock.patch.object(lifecycle_ops, "load_injects", return_value=[]), \
                mock.patch.object(lifecycle_ops, "release_held_red", return_value=0), \
                mock.patch.object(lifecycle_ops.estate_state, "EstateState") as doc, \
                mock.patch("estate.config.ssh_key_path", return_value="/k"), \
                mock.patch("red.tlaloc_ops.deploy_implants",
                           side_effect=lambda *a, **k: order.append("implants")
                           or {"red_ip": "10.0.0.242"}):
            lifecycle_ops.cmd_start(args, Path("/c"))
        return doc

    def test_implants_deploy_before_the_clock(self):
        state = {"red_staged": True, "red": {"red_ip": "10.0.0.242"},
                 "scoring_password": "pw"}
        order = []
        doc = self._start(state, order)
        self.assertEqual(order[0], "implants")
        self.assertIn("clock", order)
        self.assertTrue(state["red_deployed"])
        self.assertNotIn("red_staged", state)
        doc.load.assert_called_once()

    def test_an_armed_range_is_not_re_armed(self):
        state = {"red_deployed": True, "red": {"red_ip": "10.0.0.242"},
                 "scoring_password": "pw"}
        order = []
        self._start(state, order)
        self.assertNotIn("implants", order)


class Phase8PreparedPoint(unittest.TestCase):
    def test_it_snapshots_the_whole_range_before_arming(self):
        sys.path.insert(0, str(ROOT / "tests"))
        from _deploy_patch import dpatch
        from deploy_lib.phases import seed as dl_seed

        order = []

        class Ctx:
            from_phase, comp_dir, scoring_ip = 1, Path("/nonexistent"), "10.0.0.9"
            teams, admin_password, injects = {}, "a", []
            boxes, box_creds = [], {}   # falsy: the event.conf drift branch is not under test
            all_targets = [{"vm_name": "team1-web01", "vmid": 2001, "node": "pve",
                            "box": {"name": "web01", "template": "base-debian13-lite"}}]
            engine_vmid, node, ssh_key = 1000, "pve", "/k"
            state = {"seeded": False,
                     "red": {"red_ip": "10.0.0.242", "vmid": 1690, "node": "pve"}}

            def save_state(self):
                pass

        def note(name, value=None):
            def _f(*a, **k):
                order.append(name)
                return value
            return _f

        with dpatch("wait_for_http"), dpatch("engine_paused", return_value=False), \
                dpatch("timed", side_effect=lambda *a, **k: contextlib.nullcontext()), \
                dpatch("seed_teams", side_effect=note("seed")), \
                dpatch("unpause_engine", side_effect=note("unpause")), \
                dpatch("snap_engine", side_effect=note("snap_engine")), \
                dpatch("snap_red", side_effect=note("snap_red")), \
                dpatch("run_concurrent", side_effect=note("snap_boxes")), \
                dpatch("list_snapshots", return_value=set()), \
                dpatch("deploy_portal"), \
                mock.patch("red.tlaloc_ops.deploy_implants",
                           side_effect=note("arm", {"red_ip": "10.0.0.242"})), \
                mock.patch("red.tlaloc_ops.start_director", side_effect=note("loop")):
            dl_seed.phase8_seed(Ctx())

        # One prepared moment for every VM class, then arming, then red attacks.
        self.assertLess(order.index("snap_boxes"), order.index("arm"))
        self.assertLess(order.index("snap_red"), order.index("arm"))
        self.assertLess(order.index("snap_engine"), order.index("arm"))
        self.assertEqual(order[-2:], ["arm", "loop"])


if __name__ == "__main__":
    unittest.main()
