"""`.deploy_state.json` writers and the Linux hardening order in redeploy.

Two incidents drive this file:

* D1 (winad-testrun 2026-09-25) — `.deploy_state.json` holds the only copy of the box
  passwords. deploy.py documents the atomic-rename requirement, but three of the four
  writers (two in redeploy-competition.py, plus the one at the END of engine-recovery,
  which runs after the scoring DB was already wiped) used a plain `write_text` +
  `chmod`. A torn write there leaves a live engine with unreadable resume state and no
  second chance. All three now go through `config_ops.write_state`.

* D3 — docs/architecture.md pins the invariant that the NOPASSWD sudoers grant lands
  BEFORE the DNS fix, whose `sudo` calls soft-fail until the grant exists. redeploy had
  them inverted; it "worked" only via fix_dns_on_boxes' root guest-agent fallback, after
  burning an 8x15s retry ladder per box.

Offline; no Proxmox, no terraform, no SSH."""

import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import config_ops  # noqa: E402  (the one atomic writer; also the torn-write test's patch target)
import pipeline_api  # noqa: E402
import redeploy_engine_ops  # noqa: E402
import redeploy_light_ops  # noqa: E402
import redeploy_plant_ops  # noqa: E402
import redeploy_rebuild_ops  # noqa: E402

_REDEPLOY_SOURCES = [_REPO / "redeploy-competition.py",
                     *sorted(_REPO.glob("redeploy_*_ops.py"))]

LINUX_BOX = {"name": "web01", "template": "ubuntu-2204-web", "cpu": 2, "memory_mb": 2048}
WIN_BOX = {"name": "win01", "template": "windows-server-2022", "cpu": 2, "memory_mb": 4096}


def _target(box, vmid):
    return {
        "box": box,
        "box_name": box["name"],
        "machine": f"{box['name']}-team1",
        "team_key": "team1",
        "identifier": 104,
        "ip": f"192.168.104.{vmid % 250}",
        "vmid": vmid,
        "vm_name": f"{box['name']}-team1",
    }


LINUX = _target(LINUX_BOX, 2104)
WIN = _target(WIN_BOX, 2105)

ORIGINAL_STATE = {"box_password": "old-secret", "engine_template_vmid": 9100}
NEW_STATE = {"box_password": "new-secret", "teams": {"team1": {"password": "t1"}}}


class AtomicStateWriteTests(unittest.TestCase):
    """The state file is swapped in whole, is 0600 from the instant it exists, and a
    failed write can never damage the copy already on disk."""

    def test_redeploy_uses_the_shared_config_ops_writer(self):
        """One atomic writer in the tree, not a second local copy: the name redeploy
        imported IS config_ops.write_state, so all three sites share its guarantees."""
        for module in (redeploy_engine_ops, redeploy_light_ops, redeploy_plant_ops,
                       redeploy_rebuild_ops):
            self.assertIs(module.write_state, config_ops.write_state, module.__name__)

    def test_round_trips_is_0600_and_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".deploy_state.json"
            config_ops.write_state(path, NEW_STATE)

            self.assertEqual(json.loads(path.read_text()), NEW_STATE)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            # A leftover temp file is the signature of a torn/aborted write.
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()),
                             [".deploy_state.json"])

    def test_target_is_only_replaced_after_the_new_content_is_complete(self):
        """The torn-write proof: at the instant of the rename, the destination still
        holds the COMPLETE previous state and the temporary holds the COMPLETE new one.
        A reader therefore sees old-or-new, never a partial. The old write_text() code
        truncated the destination first, which is exactly the state this rules out."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".deploy_state.json"
            path.write_text(json.dumps(ORIGINAL_STATE))
            os.chmod(path, 0o600)
            original_bytes = path.read_bytes()

            real_replace = os.replace
            seen = {}

            def spy(src, dst):
                seen["target_at_swap"] = Path(dst).read_bytes()
                seen["tmp_at_swap"] = json.loads(Path(src).read_text())
                return real_replace(src, dst)

            with patch("os.replace", side_effect=spy):
                config_ops.write_state(path, NEW_STATE)

            self.assertEqual(seen["target_at_swap"], original_bytes)
            self.assertEqual(seen["tmp_at_swap"], NEW_STATE)
            self.assertEqual(json.loads(path.read_text()), NEW_STATE)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_failed_write_leaves_the_original_file_intact(self):
        """Simulated crash right before the rename: the resume state a live range
        depends on must still be readable, byte for byte."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".deploy_state.json"
            path.write_text(json.dumps(ORIGINAL_STATE))
            os.chmod(path, 0o600)
            original_bytes = path.read_bytes()

            with patch("os.replace", side_effect=OSError("simulated crash mid-write")):
                with self.assertRaises(OSError):
                    config_ops.write_state(path, NEW_STATE)

            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertEqual(json.loads(path.read_text()), ORIGINAL_STATE)

    def test_redeploy_has_no_hand_rolled_state_writer_left(self):
        """Guard against re-introducing one of the old writers: every state write in
        redeploy-competition.py must be the shared helper."""
        src = "\n".join(f.read_text() for f in _REDEPLOY_SOURCES)
        self.assertNotIn("state_path.write_text", src)
        self.assertNotIn("state_path.with_name", src)
        self.assertNotIn(".tmp", src)
        # Exactly the three call sites; a fourth writer would show up here.
        self.assertEqual(src.count("write_state(state_path, state)"), 3)


class StateWriteCallSiteTests(unittest.TestCase):
    """Each of the three redeploy writers actually routes through the shared helper."""

    def test_run_nakon_and_harden_persists_via_helper(self):
        ctx = {"ssh_key_path": "/tmp/id_ed25519", "scoring_engine_ip": "10.0.0.9"}
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            state_path = comp_dir / ".deploy_state.json"
            state_path.write_text(json.dumps({"box_password": "pw"}))
            state = {"box_password": "pw"}
            with patch.dict(os.environ, {"TF_VAR_vm_username": "ops"}), \
                 patch.object(pipeline_api, "setup_ubuntu_auth"), \
                 patch.object(pipeline_api, "fix_dns_on_boxes"), \
                 patch.object(pipeline_api, "ensure_nat_forwarding"), \
                 patch.object(pipeline_api, "run_nakon",
                              return_value=MagicMock(failed=[])), \
                 patch.object(pipeline_api, "fix_services_on_boxes"), \
                 patch.object(redeploy_plant_ops, "write_state") as p_write:
                cfg = comp_dir / "nakon-config.json"
                cfg.write_text(json.dumps({"machines": [{"name": LINUX["machine"]}]}))
                redeploy_plant_ops.run_nakon_and_harden(
                    [LINUX], ctx, comp_dir, state, cfg, comp_dir / "bundle")
        p_write.assert_called_once_with(state_path, state)

    def test_mode_resync_persists_via_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            state_path = comp_dir / ".deploy_state.json"
            state_path.write_text(json.dumps({"box_password": "pw"}))
            state = {"box_password": "pw"}
            with patch.object(redeploy_light_ops, "read_event_conf",
                              return_value={"postgres_password": "new"}), \
                 patch.object(redeploy_light_ops, "load_users_config", return_value=("ops", {})), \
                 patch.object(redeploy_light_ops, "write_state") as p_write:
                redeploy_light_ops.mode_resync([], {"ssh_key_path": "/k"}, "pve", comp_dir, state,
                                     state_path)
        p_write.assert_called_once_with(state_path, state)
        self.assertEqual(state["postgres_password"], "new")

    def test_engine_recovery_persists_via_helper_after_clearing_phase7_flags(self):
        """This is the D1 site: the write runs last, after the engine was re-cloned and
        the scoring DB wiped. It must also clear the stale phase-7 done-flags."""
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            state = {
                "engine_template_vmid": 9100,
                "box_password": "pw",
                "postgres_password": "pg",
                "redis_password": "rd",
                "admin_password": "ad",
                "scoring_vm_id": 1000,
                "seeded": True,
                "engine_unpaused": True,
                "injects_created": True,
            }
            with patch.dict(os.environ, {"TF_VAR_proxmox_node": "pve"}), \
                 patch.object(redeploy_engine_ops, "acquire_engine_lock"), \
                 patch.object(redeploy_engine_ops, "stored_template_hash", return_value="abc"), \
                 patch.object(redeploy_engine_ops, "args_yes_engine_recovery", return_value=True), \
                 patch.object(redeploy_engine_ops, "timed", return_value=nullcontext()), \
                 patch.object(redeploy_engine_ops, "run_terraform",
                              return_value=MagicMock(returncode=0)), \
                 patch.object(pipeline_api, "read_terraform_ctx",
                              return_value={"scoring_engine_ip": "10.0.0.9",
                                            "ssh_key_path": "/k", "vm_username": "ops"}), \
                 patch.object(redeploy_engine_ops, "forget_engine_host_key"), \
                 patch.object(redeploy_engine_ops, "wait_for_ssh"), \
                 patch.object(redeploy_engine_ops, "prepare_engine_from_template"), \
                 patch.object(redeploy_engine_ops, "push_event_conf"), \
                 patch.object(redeploy_engine_ops, "ensure_nat_forwarding"), \
                 patch.object(redeploy_engine_ops, "write_state") as p_write:
                self.assertTrue(redeploy_engine_ops.engine_recovery(
                    "comp", comp_dir, {"team1": {}}, [LINUX], state, assume_yes=True))

        p_write.assert_called_once_with(comp_dir / ".deploy_state.json", state)
        for flag in ("seeded", "engine_unpaused", "injects_created"):
            self.assertNotIn(flag, state)


class HardeningOrderTests(unittest.TestCase):
    """D3: the sudoers grant must land before the DNS fix (docs/architecture.md)."""

    def _run(self, targets):
        ctx = {"ssh_key_path": "/tmp/id_ed25519", "scoring_engine_ip": "10.0.0.9"}
        order = []
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            with patch.dict(os.environ, {"TF_VAR_vm_username": "ops"}), \
                 patch.object(pipeline_api, "setup_ubuntu_auth") as p_auth, \
                 patch.object(pipeline_api, "fix_dns_on_boxes") as p_dns, \
                 patch.object(pipeline_api, "ensure_nat_forwarding"), \
                 patch.object(pipeline_api, "run_nakon",
                              return_value=MagicMock(failed=[])), \
                 patch.object(pipeline_api, "fix_services_on_boxes"):
                p_auth.side_effect = lambda *a, **k: order.append("auth")
                p_dns.side_effect = lambda *a, **k: order.append("dns")
                cfg = comp_dir / "nakon-config.json"
                cfg.write_text(json.dumps(
                    {"machines": [{"name": t["machine"]} for t in targets]}))
                redeploy_plant_ops.run_nakon_and_harden(
                    targets, ctx, comp_dir, {}, cfg, comp_dir / "bundle")
        return order, p_auth, p_dns

    def test_auth_grant_lands_before_the_dns_fix(self):
        order, p_auth, p_dns = self._run([LINUX])
        self.assertEqual(order, ["auth", "dns"])
        # Same linux-scoped target list for both — the order is the only change.
        self.assertEqual(p_auth.call_args.args[0], [LINUX])
        self.assertEqual(p_dns.call_args.args[0], [LINUX])

    def test_order_is_stable_with_windows_boxes_mixed_in(self):
        order, _p_auth, _p_dns = self._run([LINUX, WIN])
        self.assertEqual(order, ["auth", "dns"])


if __name__ == "__main__":
    unittest.main()
