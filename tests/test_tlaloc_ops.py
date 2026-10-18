"""tlaloc_ops — the engine-side red integration: block, config, commands, plant, verdict.

No live range: every subprocess and every provisioning call is injected, so these
prove the control flow, the pushed-config contract and the fail-loud policy.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from red import gaia_ops, red01_ops, tlaloc_ops as to  # noqa: E402


class Completed:
    def __init__(self, returncode=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = returncode, out, err


def compfile(tmp, body="red.assume_breach light\n", red_yaml=None):
    d = Path(tmp) / "comp"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Compfile").write_text("name demo\n" + body)
    if red_yaml:
        (d / red_yaml).write_text("assume_breach: full\ndepth: 2\n")
    return d


class RedDiskTest(unittest.TestCase):
    """The disk resize (the C2's hard prerequisite on a live deploy)."""

    @staticmethod
    def _api(calls, size):
        def fake_api(method, path, **kwargs):
            calls.append((method, path, kwargs.get("data") or {}))
            if path == "/cluster/resources":
                return {"data": [{"vmid": 2099, "node": "proxmox"}]}
            if path.endswith("/config"):
                return {"data": {"scsi0": f"ssd:vm-2099-disk-0,size={size}"}}
            return {"data": None}
        return fake_api

    def test_the_disk_is_grown_with_put_and_an_absolute_size(self):
        calls, df = [], "/dev/mapper/ubuntu--vg-ubuntu--lv   59G  1.2G   55G   3% /\n"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("infra.client.proxmox_api", self._api(calls, "15G")):
            out = to.resize_red_disk(compfile(tmp), "10.0.0.241", 2099,
                                     ssh_key="/tmp/key", target_gb=60,
                                     runner=lambda argv, **kw: Completed(0, df))
        assert out["ok"] is True, out
        resize = [d for _, p, d in calls if p.endswith("/resize")][0]
        assert resize == {"disk": "scsi0", "size": "60G"}, resize

    def test_the_guest_grow_waits_for_the_resize_task(self):
        upid = "UPID:proxmox:1:2:3:resize:2099:x:"
        calls = []

        def api(method, path, **kwargs):
            calls.append((method, path))
            if path == "/cluster/resources":
                return {"data": [{"vmid": 2099, "node": "proxmox"}]}
            if path.endswith("/config"):
                return {"data": {"scsi0": "ssd:vm-2099-disk-0,size=15G"}}
            return {"data": upid}

        waited, df = [], "/dev/mapper/ubuntu--vg-ubuntu--lv   59G  1.2G   55G   3% /\n"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("infra.client.proxmox_api", api), \
                mock.patch("infra.client.wait_for_proxmox_task",
                           side_effect=lambda node, upid, timeout=None:
                           waited.append((node, upid))):
            out = to.resize_red_disk(compfile(tmp), "10.0.0.241", 2099,
                                     ssh_key="/tmp/key", target_gb=60,
                                     runner=lambda argv, **kw: Completed(0, df))
        assert out["ok"] is True, out
        assert waited == [("proxmox", upid)], waited

    def test_a_disk_already_at_the_floor_is_left_alone(self):
        calls, df = [], "/dev/mapper/ubuntu--vg-ubuntu--lv   59G  1.2G   55G   3% /\n"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("infra.client.proxmox_api", self._api(calls, "60G")):
            out = to.resize_red_disk(compfile(tmp), "10.0.0.241", 2099,
                                     ssh_key="/tmp/key", target_gb=60,
                                     runner=lambda argv, **kw: Completed(0, df))
        assert out["ok"] is True and out["grew"] is False, out
        assert not [p for _, p, _ in calls if p.endswith("/resize")], calls

    def test_the_guest_grow_runs_through_sudo(self):
        script = to._guest_grow_script()
        for cmd in ("growpart", "pvresize", "lvextend", "resize2fs"):
            assert f"sudo -n {cmd}" in script, (cmd, script)
        assert script.rstrip().endswith("df -h /"), script

    def test_the_partition_comes_from_lvm_not_a_walk_up_the_device_tree(self):
        script = to._guest_grow_script()
        assert "pvs --noheadings -o pv_name" in script, script
        assert "/sys/class/block/$(basename \"$PV\")/partition" in script, script

    def test_a_grown_disk_whose_filesystem_was_not_extended_is_not_ok(self):
        calls, df = [], "/dev/mapper/ubuntu--vg-ubuntu--lv  9.8G  5.2G  4.1G  57% /\n"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("infra.client.proxmox_api", self._api(calls, "15G")):
            out = to.resize_red_disk(compfile(tmp), "10.0.0.241", 2099,
                                     ssh_key="/tmp/key", target_gb=60,
                                     runner=lambda argv, **kw: Completed(0, df))
        assert out["ok"] is False, out
        assert out["after_gb"] == 9, out


class RedBlockTests(unittest.TestCase):
    def test_dotted_keys_and_the_yaml_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp, "red.assume_breach light\nred.depth 2\n"
                                 "red.caps.max_down 4\n")
            block = to.red_block(comp)
        self.assertEqual(block["assume_breach"], "light")
        self.assertEqual(block["depth"], 2)
        self.assertEqual(block["caps"], {"max_down": 4})

    def test_compfile_keys_win_over_the_yaml_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp, "red_yaml red.yaml\nred.depth 1\n", red_yaml="red.yaml")
            block = to.red_block(comp)
        self.assertEqual(block["assume_breach"], "full")
        self.assertEqual(block["depth"], 1)

    def test_validation_flags_every_bad_field(self):
        problems = to.validate_red_block({
            "assume_breach": "maybe", "depth": 9, "duration_min": -1,
            "ladder": {"tiers": [{"name": "t", "at_pct": 0.5, "modes": ["nope"]}]},
            "caps": {"min_standing": 0}})
        fields = {p["field"] for p in problems}
        self.assertIn("red.assume_breach", fields)
        self.assertIn("red.depth", fields)
        self.assertIn("red.duration_min", fields)
        self.assertIn("red.caps.min_standing", fields)

    def test_a_good_block_validates_clean(self):
        self.assertEqual(to.validate_red_block({"assume_breach": "full", "depth": 3}), [])


class ConfigAndCommandTests(unittest.TestCase):
    def test_config_carries_the_intel_dir_as_the_comp_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp)
            cfg = to.tlaloc_config(comp, comp_id="demo", engine_ip="10.0.0.92",
                                   clef_url="http://10.0.0.5:8080")
        self.assertEqual(cfg["run"]["comp"], "demo")
        self.assertEqual(cfg["run"]["comp_dir"], to.INTEL_DIR)
        self.assertEqual(cfg["run"]["state_dir"], to.STATE_DIR)
        self.assertEqual(cfg["sensors"]["quotient_url"], "http://10.0.0.92")
        self.assertEqual(cfg["clef"]["base_url"], "http://10.0.0.5:8080")

    def test_the_seed_and_run_are_given_the_intel_comp_dir(self):
        seed, run, report = (to.seed_command(), to.plant_command(), to.report_command())
        for cmd in (seed, run, report):
            self.assertIn(f"--comp {to.INTEL_DIR}", cmd)
            self.assertIn(f"cd {to.INSTALL_DIR}", cmd)
        self.assertIn(f"--config {to.CONFIG_PATH}", seed)

    def test_comp_prep_is_absolute_and_reads_the_rendered_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp)
            argv = to.comp_prep_command(comp)
        self.assertIn(str(comp.resolve()), argv)
        self.assertIn(str(comp.resolve() / ".tlaloc-config.yaml"), argv)

    def test_the_rendered_config_is_fed_to_the_push(self):
        seen = {}

        def fake_ssh(key, ip, remote, **kw):
            seen["kw"] = kw
            return Completed(0, "", "")

        with mock.patch.object(to, "_ssh_run", fake_ssh):
            to.push_config("/k", "10.0.0.241", "run:\n  comp: demo\n")
        self.assertEqual(seen["kw"]["input"], "run:\n  comp: demo\n")

    def test_c2_prep_gets_a_writable_operator_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp)
            argv = to.comp_prep_command(comp)
        self.assertIn("--state-dir", argv)
        self.assertIn(str(comp.resolve() / ".tlaloc-state"), argv)
        self.assertIn(str(comp.resolve() / ".tlaloc-config.yaml"), argv)

    def test_ssh_argv_is_strict_and_bounded(self):
        argv = to.ssh_argv("/k", "10.0.0.241", "true", timeout=12)
        self.assertIn("StrictHostKeyChecking=no", argv)
        self.assertIn("UserKnownHostsFile=/dev/null", argv)
        self.assertIn("ConnectTimeout=12", argv)
        self.assertEqual(argv[-1], "true")


class FakeCtx:
    def __init__(self, comp):
        from infra.ownership import Ownership
        self.comp_dir = comp
        self.comp_id = "demo"
        self.comp_name = "demo"
        self.run_id = "run-test"
        self.ssh_key_abs = "/k"
        self.engine_vmid = 1300
        self.scoring_ip = "10.0.0.92"
        self.scoring_user = "sysadmin"
        self.node = "pve"
        self.placement = None
        self.teams = {"team1": {"identifier": 101}}
        self.state = {}
        self.saved = 0
        self.ownership = Ownership(self.comp_name, self.run_id)

    def save_state(self):
        self.saved += 1


class PlantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.comp = compfile(self.tmp.name, "red.c2 off\n")
        self.ctx = FakeCtx(self.comp)
        self.runner = lambda argv, **kw: Completed(0, "[]")

    def _patch(self, deploy=None, seed_via_red01=None, verify_config_contract=None):
        values = {
            "resize_red_disk": {"return_value": {"ok": True, "before_gb": 15,
                                                "after_gb": 60}},
            "stage_red01": {"return_value": {"ok": True}},
            "push_config": {"return_value": {"ok": True}},
            "install_unit": {"return_value": {"ok": True}},
            "verify_config_contract": verify_config_contract or {"return_value": {"ok": True}},
            "seed_via_red01": seed_via_red01 or {"return_value": {"ok": True}},
            "pull_manifest": {"return_value": {
                "path": str(self.comp / "red" / "manifest.json")}},
            "push_llm_env": {"return_value": {"ok": True}},
            "comp_prep_command": {"return_value": ["tlaloc", "c2-prep"]},
        }
        for name, kw in values.items():
            patcher = mock.patch.object(to, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, kw in (("deploy", deploy or {"return_value": {
                             "ok": True, "vmid": 1690, "ip": "10.0.0.241",
                             "node": "pve"}}),
                         ("record_identity", {}), ("install_engine_rules", {})):
            patcher = mock.patch.object(red01_ops, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(gaia_ops, "mythic_enabled", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        manifest = self.comp / "red" / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(
            {"boxes": {"192.168.101.2": {"stages": [{"stage": "access"}]}}}))

    def test_it_provisions_red01_and_records_the_identity(self):
        self._patch()
        out = to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner)
        red01_ops.deploy.assert_called_once()
        red01_ops.record_identity.assert_called_once()
        self.assertEqual(out["vmid"], 1690)
        self.assertTrue((self.comp / ".tlaloc-config.yaml").exists())
        self.assertEqual(self.ctx.state["red"]["red_ip"], "10.0.0.241")
        self.assertEqual(self.ctx.saved, 1)

    def test_the_config_is_written_for_the_competition_and_staged(self):
        self._patch()
        to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner)
        self.assertTrue((self.comp / ".tlaloc-config.yaml").exists())
        to.stage_red01.assert_called_once()
        to.push_config.assert_called_once()
        to.seed_via_red01.assert_called_once()

    def test_an_invalid_red_block_fails_before_anything_is_created(self):
        comp = compfile(self.tmp.name + "bad", "red.assume_breach maybe\n")
        ctx = FakeCtx(comp)
        with self.assertRaises(RuntimeError) as cm:
            to.plant_assume_breach(ctx, ssh_key="/k", runner=self.runner)
        self.assertIn("red.assume_breach", str(cm.exception))

    def test_a_failed_provision_fails_the_deploy(self):
        self._patch(deploy={"side_effect": RuntimeError("boom")})
        with self.assertRaises(RuntimeError):
            to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner)

    def test_a_failed_seed_fails_the_deploy(self):
        self._patch(seed_via_red01={"side_effect": RuntimeError("no seed")})
        with self.assertRaises(RuntimeError):
            to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner)

    def test_a_dropped_contract_field_fails_the_deploy(self):
        self._patch(verify_config_contract={"side_effect": RuntimeError("dropped")})
        with self.assertRaises(RuntimeError):
            to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner)

    def test_the_mythic_step_runs_before_c2_prep_and_lands_its_keys(self):
        self._patch()
        plan = {"box": "10.0.0.241", "callback_host": "192.168.101.1",
                "callback_port": 80, "operation": "demo"}
        calls = []
        c2 = {"ok": True, "c2": {"enabled": True, "callback_port": 80},
              "env": "TLALOC_MYTHIC_PASSWORD=x\n"}
        with mock.patch.object(gaia_ops, "mythic_enabled", return_value=True), \
                mock.patch.object(gaia_ops, "c2_plan", return_value=plan), \
                mock.patch.object(gaia_ops, "prepare_mythic",
                                  side_effect=lambda *a, **k: calls.append("mythic") or c2), \
                mock.patch.object(gaia_ops, "apply_c2_to_config",
                                  side_effect=lambda *a, **k: calls.append("apply")), \
                mock.patch.object(gaia_ops, "push_env", return_value={"ok": True}), \
                mock.patch.object(to, "comp_prep_command",
                                  side_effect=lambda *a, **k: calls.append("prep") or ["x"]):
            to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner)
        self.assertEqual(calls, ["mythic", "apply", "prep"])
        red01_ops.install_engine_rules.assert_called_once()

    def test_a_missing_callback_port_fails_before_the_install(self):
        self._patch()
        plan = {"box": "10.0.0.241", "callback_host": "192.168.101.1",
                "callback_port": None, "operation": "demo"}
        with mock.patch.object(gaia_ops, "mythic_enabled", return_value=True), \
                mock.patch.object(gaia_ops, "c2_plan", return_value=plan):
            with self.assertRaises(RuntimeError) as cm:
                to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner)
        self.assertIn("callback port", str(cm.exception))

    def test_the_llm_env_is_pushed_only_when_a_key_is_given(self):
        self._patch()
        to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner)
        to.push_llm_env.assert_not_called()
        to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner,
                               llm={"enabled": True}, llm_key="sk-x")
        to.push_llm_env.assert_called_once()


class VerdictTests(unittest.TestCase):
    def test_collect_evidence_copies_only_what_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "restore-table.json").write_text(
                json.dumps({"summary": {"takedowns": 2}, "entries": [1, 2]}))
            (state / "report.md").write_text("# report")
            out = to.collect_evidence(state, Path(tmp) / "ev")
        self.assertEqual(sorted(out), ["report.md", "restore-table.json"])

    def test_verdict_reads_the_agents_own_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = Path(tmp)
            (ev / "restore-table.json").write_text(
                json.dumps({"summary": {"takedowns": 3}, "entries": [1, 2, 3],
                            "commands": [1]}))
            (ev / "report.md").write_text("# r")
            (ev / "events.jsonl").write_text("{}\n")
            verdict = to.write_verdict(ev)
            self.assertTrue((ev / "verdict.json").exists())
        self.assertEqual(verdict["entries"], 3)
        self.assertEqual(verdict["commands"], 1)
        self.assertEqual(verdict["summary"], {"takedowns": 3})
        self.assertTrue(verdict["report"].endswith("report.md"))

    def test_verdict_without_artifacts_is_empty_not_wrong(self):
        with tempfile.TemporaryDirectory() as tmp:
            verdict = to.red_verdict(Path(tmp))
        self.assertEqual((verdict["entries"], verdict["restore_table"]), (0, None))


class StatusTests(unittest.TestCase):
    def test_status_reports_the_block_and_recorded_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp)
            status = to.status(comp)
        self.assertEqual(status["engine"], "tlaloc")
        self.assertIn("assume_breach", status["block"])
        self.assertEqual(status["identity"], {"vmid": None, "ip": "", "node": None})


if __name__ == "__main__":
    unittest.main()


class OperatorCredentials(unittest.TestCase):
    def test_the_operator_password_is_kept_for_a_resume(self):
        import os
        import stat
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            comp = Path(tmp) / "comp"
            fresh = {"env": "TLALOC_MYTHIC_PASSWORD=opw\nMYTHIC_HOST=10.0.0.195\n",
                     "admin_password": "adminpw"}
            with mock.patch.object(Path, "home", return_value=home):
                self.assertEqual(
                    to._operator_env(comp, fresh)["TLALOC_MYTHIC_PASSWORD"], "opw")
                # a resume whose Gaia re-run only offered the admin fallback still
                # authenticates as the operator that was minted first
                stale = {"env": "TLALOC_MYTHIC_PASSWORD=adminpw\n",
                         "admin_password": "adminpw"}
                self.assertEqual(
                    to._operator_env(comp, stale)["TLALOC_MYTHIC_PASSWORD"], "opw")
                store = home / ".tezcatlipoca" / "c2" / "comp.env"
                self.assertEqual(stat.S_IMODE(os.stat(store).st_mode), 0o600)
