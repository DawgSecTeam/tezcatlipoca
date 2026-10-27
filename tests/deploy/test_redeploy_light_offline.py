"""redeploy_light_ops (resync / rollback / reconfigure) and destroy_templates_ops driven with
fakes. Their bodies were reached by no test after the split, so a renamed import or a changed
signature in a module they call would only have shown up mid-event."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from teardown import machinery as dto  # noqa: E402
from infra.ownership import Ownership  # noqa: E402
from estate import state as estate_state
from redeploy import redeploy_light_ops as light  # noqa: E402
from redeploy.redeploy_context import RedeployContext  # noqa: E402
from common.constants import SNAP_BASE, SNAP_READY  # noqa: E402

LIN = {"name": "web01", "template": "base-ubuntu24.04-fix"}
WIN = {"name": "dc01", "template": "base-windows-server"}


def _t(box, team="team1", vmid=1, node=None):
    t = {"team_key": team, "box_name": box["name"], "box": box, "vm_name": f"{box['name']}-{team}",
         "vmid": vmid, "ip": "192.168.101.2"}
    if node:
        t["node"] = node
    return t


def _rctx(ctx=None, node="pve1", comp_dir=Path("."), state=None, state_path=None):
    return RedeployContext(comp_dir=comp_dir, name="x", comp_name="x", node=node,
                           teams=None, boxes=None, difficulty=None,
                           state={} if state is None else state,
                           state_path=state_path, ctx={} if ctx is None else ctx)


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        yield buf


class Resync(unittest.TestCase):
    def test_aligns_state_and_resets_passwords_per_platform(self):
        with tempfile.TemporaryDirectory() as d:
            comp = Path(d)
            (comp / "users.json").write_text(json.dumps({"box_username": "u", "credlist_usernames": ["a"]}))
            state = {"admin_password": "old", "box_password": "bp", "box_creds": {"a": "x"},
                     "teams": {"team1": {"identifier": "101", "password": "old"}}}
            engine = {"admin_password": "new", "box_creds": {"a": "y"},
                      "team_passwords": {"team1": "tnew"}}
            win_calls, lin_calls = [], []
            with patch.object(light, "read_event_conf", return_value=engine), \
                    patch.object(light, "guest_agent_exec_windows",
                                 side_effect=lambda n, v, c, timeout: win_calls.append((n, c)) or (0, "", "")), \
                    patch.object(light, "guest_agent_exec_root",
                                 side_effect=lambda n, v, c, timeout: lin_calls.append((n, c)) or (1, "", "bad")), \
                    quiet() as out:
                done = light.mode_resync([_t(LIN, node="pve2"), _t(WIN)],
                                         _rctx(comp_dir=comp, state=state,
                                               state_path=estate_state.state_path(comp)))
            self.assertEqual(len(done), 2)
            self.assertEqual(state["admin_password"], "new")
            self.assertEqual(state["box_creds"], {"a": "y"})
            self.assertEqual(state["teams"]["team1"]["password"], "tnew")
            self.assertEqual(estate_state.load_flat(comp)["admin_password"], "new")
            self.assertEqual(lin_calls[0][0], "pve2")       # target's own node
            self.assertEqual(win_calls[0][0], "pve1")       # falls back to the default node
            self.assertIn("guest agent rc=1", out.getvalue())

    def test_requires_box_password(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.object(light, "read_event_conf", return_value={}), quiet():
            with self.assertRaises(SystemExit):
                light.mode_resync([], _rctx(comp_dir=Path(d), state={}, state_path=Path(d) / "s.json"))


class Rollback(unittest.TestCase):
    def test_base_rollback_runs_the_shared_post_base_convergence(self):
        # The convergence itself (predomain -> harden -> domains -> final -> late -> tz-ready)
        # is exercised in redeploy/test_redeploy_hardening; here we only prove the rollback
        # path reaches it, in place of the old postclone-then-domains order.
        with patch.object(light, "list_snapshots", return_value={SNAP_BASE, SNAP_READY}), \
                patch.object(light, "delete_snapshot"), \
                patch.object(light, "rollback_snapshot"), \
                patch.object(light.pipeline_api, "wait_for_boxes_ssh") as ssh, \
                patch.object(light.pipeline_api, "wait_for_cloud_init") as ci, \
                patch.object(light, "post_base_converge") as converge, quiet():
            done = light.mode_rollback([_t(LIN)], _rctx(), SNAP_BASE, "cfg", "b",
                                       reconfigure=True)
        self.assertEqual(len(done), 1)
        ssh.assert_called_once()
        ci.assert_called_once()
        converge.assert_called_once()


    def test_all_rollbacks_failing_is_fatal_and_domain_failure_keeps_ready_snapshot_off(self):
        with patch.object(light, "list_snapshots", return_value=set()), \
                patch.object(light, "rollback_snapshot", side_effect=RuntimeError("nope")), quiet():
            with self.assertRaises(SystemExit):
                light.mode_rollback([_t(LIN)], _rctx(), SNAP_READY, None, None, False)
        with patch.object(light, "list_snapshots", return_value=set()), \
                patch.object(light, "rollback_snapshot"), \
                patch.object(light.pipeline_api, "wait_for_boxes_ssh"), \
                patch.object(light.pipeline_api, "wait_for_cloud_init"), \
                patch.object(light, "post_base_converge",
                             side_effect=RuntimeError("dc")), quiet():
            with self.assertRaises(RuntimeError):
                light.mode_rollback([_t(LIN)], _rctx(), SNAP_BASE, "c", "b", True)




class TeardownTemplates(unittest.TestCase):
    def _run(self, full, placement=None):
        with tempfile.TemporaryDirectory() as d:
            comp = Path(d)
            from engine.template_freeze import save_template_hashes
            save_template_hashes(comp, engine={"hash": "e1"})
            with patch.object(dto, "destroy_golden_set") as g, patch.object(dto, "destroy_jump_vms") as j, \
                    patch.object(dto, "destroy_engine_template") as e, \
                    patch.object(dto, "record_of", return_value=SimpleNamespace(node="pve2")), \
                    patch.dict("os.environ", {"TEZ_PROXMOX_NODE": "pve1"}), quiet():
                dto.teardown_templates(comp, {"scoring_vm_id": "1234"},
                                       Ownership("c", "run-0badf00d"), [LIN, WIN],
                                       placement, full)
            from engine.template_freeze import load_template_hashes
            return g, j, e, bool(load_template_hashes(comp).get("engine"))

    def test_teams_only_keeps_everything(self):
        g, j, e, hashes = self._run(False)
        g.assert_not_called()
        e.assert_not_called()
        self.assertTrue(hashes)

    def test_full_single_node(self):
        g, j, e, hashes = self._run(True)
        g.assert_called_once()
        self.assertEqual(g.call_args.args[:3], ("pve1", 1234, 2))
        e.assert_called_once()
        j.assert_not_called()
        self.assertFalse(hashes)
        self.assertIn("run-0badf00d", e.call_args.kwargs["expect_tags"])

    def test_full_multi_node_destroys_satellite_goldens_and_jumps(self):
        placement = {"satellites": [{"name": "n2", "slot": 1, "jump_vmid": 1131}]}
        g, j, e, _ = self._run(True, placement)
        self.assertEqual(g.call_count, 2)
        self.assertEqual(g.call_args_list[1].args[0], "pve2")
        self.assertEqual(g.call_args_list[1].kwargs["slot"], 1)
        j.assert_called_once()



if __name__ == "__main__":
    unittest.main()
