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

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import destroy_templates_ops as dto  # noqa: E402
import redeploy_light_ops as light  # noqa: E402
from constants import SNAP_BASE, SNAP_READY  # noqa: E402

LIN = {"name": "web01", "template": "base-ubuntu24.04-fix"}
WIN = {"name": "dc01", "template": "base-windows-server"}


def _t(box, team="team1", vmid=1, node=None):
    t = {"team_key": team, "box_name": box["name"], "box": box, "vm_name": f"{box['name']}-{team}",
         "vmid": vmid, "ip": "192.168.101.2"}
    if node:
        t["node"] = node
    return t


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
                done = light.mode_resync([_t(LIN, node="pve2"), _t(WIN)], {}, "pve1", comp, state,
                                         comp / ".deploy_state.json")
            self.assertEqual(len(done), 2)
            self.assertEqual(state["admin_password"], "new")
            self.assertEqual(state["box_creds"], {"a": "y"})
            self.assertEqual(state["teams"]["team1"]["password"], "tnew")
            self.assertEqual(json.loads((comp / ".deploy_state.json").read_text())["admin_password"], "new")
            self.assertEqual(lin_calls[0][0], "pve2")       # target's own node
            self.assertEqual(win_calls[0][0], "pve1")       # falls back to the default node
            self.assertIn("guest agent rc=1", out.getvalue())

    def test_requires_box_password(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.object(light, "read_event_conf", return_value={}), quiet():
            with self.assertRaises(SystemExit):
                light.mode_resync([], {}, "pve1", Path(d), {}, Path(d) / "s.json")


class Rollback(unittest.TestCase):
    def test_base_rollback_drops_newer_ready_snapshot_first(self):
        order = []
        with patch.object(light, "list_snapshots", return_value={SNAP_BASE, SNAP_READY}), \
                patch.object(light, "delete_snapshot", side_effect=lambda *a: order.append(("del", a[2]))), \
                patch.object(light, "rollback_snapshot", side_effect=lambda *a: order.append(("roll", a[2]))), \
                patch.object(light.pipeline_api, "wait_for_boxes_ssh") as ssh, \
                patch.object(light.pipeline_api, "wait_for_cloud_init") as ci, \
                patch.object(light, "run_nakon_and_harden") as harden, \
                patch.object(light, "rerun_domain_configs", return_value=True), \
                patch.object(light, "take_snapshot") as snap, quiet():
            done = light.mode_rollback([_t(LIN)], {}, "pve1", SNAP_BASE, Path("."), {}, "cfg", "b",
                                       reconfigure=True)
        self.assertEqual(order, [("del", SNAP_READY), ("roll", SNAP_BASE)])
        self.assertEqual(len(done), 1)
        ssh.assert_called_once()
        ci.assert_called_once()
        harden.assert_called_once()
        snap.assert_called_once()
        self.assertEqual(snap.call_args.args[2], SNAP_READY)

    def test_ready_rollback_does_not_reconfigure_or_retake(self):
        with patch.object(light, "list_snapshots", return_value={SNAP_READY}), \
                patch.object(light, "rollback_snapshot"), \
                patch.object(light.pipeline_api, "wait_for_boxes_ssh"), \
                patch.object(light, "run_nakon_and_harden") as harden, \
                patch.object(light, "take_snapshot") as snap, quiet():
            light.mode_rollback([_t(LIN)], {}, "pve1", SNAP_READY, Path("."), {}, None, None,
                                reconfigure=False)
        harden.assert_not_called()
        snap.assert_not_called()

    def test_all_rollbacks_failing_is_fatal_and_domain_failure_keeps_ready_snapshot_off(self):
        with patch.object(light, "list_snapshots", return_value=set()), \
                patch.object(light, "rollback_snapshot", side_effect=RuntimeError("nope")), quiet():
            with self.assertRaises(SystemExit):
                light.mode_rollback([_t(LIN)], {}, "pve1", SNAP_READY, Path("."), {}, None, None, False)
        with patch.object(light, "list_snapshots", return_value=set()), \
                patch.object(light, "rollback_snapshot"), \
                patch.object(light.pipeline_api, "wait_for_boxes_ssh"), \
                patch.object(light.pipeline_api, "wait_for_cloud_init"), \
                patch.object(light, "run_nakon_and_harden"), \
                patch.object(light, "rerun_domain_configs", side_effect=RuntimeError("dc")), \
                patch.object(light, "take_snapshot") as snap, quiet():
            with self.assertRaises(RuntimeError):
                light.mode_rollback([_t(LIN)], {}, "pve1", SNAP_BASE, Path("."), {}, "c", "b", True)
        snap.assert_not_called()


class Reconfigure(unittest.TestCase):
    def test_reconfigure_runs_chain_and_notes_domain(self):
        with tempfile.TemporaryDirectory() as d:
            comp = Path(d)
            (comp / "domain_roles.json").write_text(json.dumps({"dc01": "dc"}))
            with patch.object(light.pipeline_api, "wait_for_boxes_ssh") as ssh, \
                    patch.object(light, "run_nakon_and_harden") as harden, quiet() as out:
                done = light.mode_reconfigure([_t(WIN)], {}, comp, {}, "cfg", "bundle")
            self.assertEqual(len(done), 1)
            self.assertEqual(ssh.call_args.kwargs["timeout"], 900)
            harden.assert_called_once()
            self.assertIn("domain membership is assumed", out.getvalue())


class TeardownTemplates(unittest.TestCase):
    def _run(self, full, placement=None):
        with tempfile.TemporaryDirectory() as d:
            comp = Path(d)
            (comp / ".template-hashes.json").write_text("{}")
            with patch.object(dto, "destroy_golden_set") as g, patch.object(dto, "destroy_jump_vms") as j, \
                    patch.object(dto, "destroy_engine_template") as e, \
                    patch.object(dto, "record_of", return_value=SimpleNamespace(node="pve2")), \
                    patch.dict("os.environ", {"TF_VAR_proxmox_node": "pve1"}), quiet():
                dto.teardown_templates(comp, "c", "run-0badf00d", [LIN, WIN], {"scoring_vm_id": "1234"},
                                       placement, full)
            return g, j, e, (comp / ".template-hashes.json").exists()

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

    def test_bad_state_vmid_falls_back_to_default(self):
        from constants import SCORING_ENGINE_VMID
        self.assertEqual(dto.engine_vmid_from_state({"scoring_vm_id": "x"}), SCORING_ENGINE_VMID)
        self.assertEqual(dto.engine_vmid_from_state({}), SCORING_ENGINE_VMID)


if __name__ == "__main__":
    unittest.main()
