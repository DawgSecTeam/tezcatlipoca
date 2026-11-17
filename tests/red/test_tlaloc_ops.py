"""tlaloc_ops — the engine-side red integration: block, config, commands, plant, verdict.

No live range: every subprocess and every provisioning call is injected, so these
prove the control flow, the pushed-config contract and the fail-loud policy.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from red import gaia_ops, red01_ops, sliver_ops, tlaloc_ops as to  # noqa: E402


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

    def test_the_state_is_keyed_by_the_competition_not_the_workspace_dir(self):
        # A dashboard workspace is <run>/comp, so a dir-derived name would call this range
        # "comp" while a manual run of the same comp calls it "scrim-one" — the same range under
        # two identities, which is what refused a seeded .tlaloc-state as cross-event state.
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp)
            cfg = to.tlaloc_config(comp)
        self.assertEqual(cfg["run"]["comp"], "demo")

    def test_the_rendered_config_carries_the_selected_c2_engine(self):
        # Without this key tlaloc defaults to sliver, so a Mythic comp's c2-prep validates
        # its http,webhook list against Sliver's transports (and red01 builds the wrong client).
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp, "red.c2_engine mythic\n")
            cfg = to.tlaloc_config(comp, c2_engine="mythic")
        self.assertEqual(cfg["c2"]["engine"], "mythic")

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

    def test_a_held_unit_is_installed_disabled_and_reopening_enables_it(self):
        # An enabled-but-stopped unit is not a hold: the next boot starts red by itself, and a
        # prepared reset is a boot. So the held state is a disabled unit, and the release has
        # to make it boot-live as it starts it.
        seen = []

        def fake_ssh(key, ip, remote, **kw):
            seen.append(remote)
            return Completed(0, "", "")

        with mock.patch.object(to, "_ssh_run", fake_ssh):
            to.install_unit("/k", "10.0.0.241", enable=False)
            to.install_unit("/k", "10.0.0.241")
            to.start_unit("/k", "10.0.0.241")
        self.assertIn("systemctl disable tlaloc.service", seen[0])
        self.assertIn("systemctl enable tlaloc.service", seen[1])
        self.assertIn("systemctl enable --now tlaloc.service", seen[2])



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
    CLEF = "red.clef_url http://10.0.0.9:8080\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.comp = compfile(self.tmp.name, "red.c2 off\nred.c2_engine mythic\n" + self.CLEF)
        self.ctx = FakeCtx(self.comp)
        self.runner = lambda argv, **kw: Completed(0, "[]")

    def _patch(self, deploy=None, seed_via_red01=None, verify_config_contract=None):
        values = {
            "resize_red_disk": {"return_value": {"ok": True, "before_gb": 15,
                                                "after_gb": 60}},
            "stage_red01": {"return_value": {"ok": True}},
            "push_config": {"return_value": {"ok": True}},
            "install_unit": {"return_value": {"ok": True}},
            "start_unit": {"return_value": {"ok": True}},
            "verify_config_contract": verify_config_contract or {"return_value": {"ok": True}},
            "seed_via_red01": seed_via_red01 or {"return_value": {"ok": True}},
            "pull_manifest": {"return_value": {
                "path": str(self.comp / "red" / "manifest.json")}},
            "bring_up_listeners": {"return_value": Completed(0, "")},
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
            {"boxes": {"192.168.101.2": {"stages": ["access", "channel"]}}}))

    def test_assume_breach_off_skips_the_seed_and_manifest(self):
        self.comp = compfile(self.tmp.name + "off", "red.assume_breach off\nred.c2 off\n"
                                    "red.clef_url http://clef.test\n")
        self.ctx = FakeCtx(self.comp)
        self._patch()
        out = to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner)
        to.seed_via_red01.assert_not_called()
        to.pull_manifest.assert_not_called()
        to.start_unit.assert_called_once()
        self.assertIsNone(out["manifest"])

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
        to.start_unit.assert_called_once()

    def test_an_invalid_red_block_fails_before_anything_is_created(self):
        comp = compfile(self.tmp.name + "bad", "red.assume_breach maybe\n")
        ctx = FakeCtx(comp)
        with self.assertRaises(RuntimeError) as cm:
            to.plant_assume_breach(ctx, ssh_key="/k", runner=self.runner)
        self.assertIn("red.assume_breach", str(cm.exception))

    def test_a_missing_decision_model_endpoint_fails_the_deploy(self):
        """A placeholder endpoint is not a degradation: decisions would silently
        fall back to deterministic for the whole event."""
        comp = compfile(self.tmp.name + "noclef", "red.assume_breach light\n")
        ctx = FakeCtx(comp)
        with mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop("TEZ_CLEF_URL", None)
            with self.assertRaises(RuntimeError) as cm:
                to.plant_assume_breach(ctx, ssh_key="/k", runner=self.runner)
        self.assertIn("decision-model endpoint", str(cm.exception))

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

    def test_each_deploy_mints_its_own_mythic_operator(self):
        self._patch()
        plan = {"box": "10.0.0.241", "callback_host": "192.168.101.1",
                "callback_port": 80, "operation": "demo"}
        seen = []
        c2 = {"ok": True, "c2": {"enabled": True, "callback_port": 80},
              "env": "TLALOC_MYTHIC_PASSWORD=x\nMYTHIC_USER=tlaloc-abc\n"}
        with mock.patch.object(gaia_ops, "mythic_enabled", return_value=True), \
                mock.patch.object(gaia_ops, "c2_plan", return_value=plan), \
                mock.patch.object(gaia_ops, "prepare_mythic",
                                  side_effect=lambda *a, **k: seen.append(k.get("agent_user")) or c2), \
                mock.patch.object(gaia_ops, "apply_c2_to_config"), \
                mock.patch.object(gaia_ops, "push_env", return_value={"ok": True}), \
                mock.patch.object(to, "comp_prep_command", return_value=["x"]):
            to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner)
        self.assertTrue(seen and seen[0].startswith("tlaloc-"), seen)

    def test_a_missing_callback_port_fails_before_the_install(self):
        self._patch()
        plan = {"box": "10.0.0.241", "callback_host": "192.168.101.1",
                "callback_port": None, "operation": "demo"}
        with mock.patch.object(gaia_ops, "mythic_enabled", return_value=True), \
                mock.patch.object(gaia_ops, "c2_plan", return_value=plan):
            with self.assertRaises(RuntimeError) as cm:
                to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner)
        self.assertIn("callback port", str(cm.exception))

    def test_the_llm_env_is_pushed_when_a_key_or_the_gateway_key_is_present(self):
        self._patch()
        to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner)
        to.push_llm_env.assert_not_called()
        to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner,
                               llm={"enabled": True}, llm_key="sk-x")
        to.push_llm_env.assert_called_once()
        # A create-competition deploy passes no llm_key, but a gateway-pinned clef still
        # needs the bearer the service env carries.
        to.push_llm_env.reset_mock()
        with mock.patch.dict(os.environ, {"TEZ_LITELLM_API_KEY": "sk-gw"}):
            to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner)
        to.push_llm_env.assert_called_once()
        self.assertEqual(to.push_llm_env.call_args.args[2], "sk-gw")


    def test_an_operator_can_extend_the_event_without_editing_the_comp(self):
        """A range kept up for human blue testing goes quiet at duration_min;
        TEZ_TLALOC_DURATION_MIN extends the clock without touching the comp."""
        with mock.patch.dict(os.environ, {"TEZ_TLALOC_DURATION_MIN": "480"}):
            block = to.red_block(self.comp)
        self.assertEqual(float(block["duration_min"]), 480.0)

    def test_prepare_only_leaves_no_implant_on_a_box(self):
        # The prepared restore points are taken on a range nobody has compromised: the C2
        # and its frozen matrix exist on red01, and the seed is the event-start half.
        self._patch()
        out = to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner,
                                    deploy_implants_now=False)
        to.seed_via_red01.assert_not_called()
        to.pull_manifest.assert_not_called()
        to.start_unit.assert_not_called()
        self.assertIsNone(out["manifest"])
        self.assertTrue(self.ctx.state["red_staged"])
        self.assertNotIn("red_deployed", self.ctx.state)
        # The unit goes in disabled: these restore points are rebooted on every reset, and a
        # unit enabled at boot would arm the range before the event does.
        self.assertIs(to.install_unit.call_args.kwargs["enable"], False)

    def test_a_reused_matrix_still_brings_the_listeners_up(self):
        # A prepared reset rolls red01 back and reboots it. The payloads survive on disk;
        # what answers them does not — a Sliver server restores its listeners at daemon
        # start, on whichever of the box's servers got there first. Skipping the freeze
        # cannot mean skipping the listeners the freeze would have created.
        self._patch()
        self.comp = compfile(self.tmp.name + "slv", "red.clef_url http://10.0.0.9:8080\n")
        self.ctx = FakeCtx(self.comp)
        manifest = self.comp / "red" / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(
            {"boxes": {"192.168.101.2": {"stages": ["access", "channel"]}}}))
        with mock.patch.object(sliver_ops, "sliver_enabled", return_value=True), \
             mock.patch.object(sliver_ops, "c2_plan", return_value={
                 "box": "10.0.0.241", "ports": {}, "domain": "c2.demo."}), \
             mock.patch.object(sliver_ops, "prepare_sliver",
                               return_value={"ok": True, "c2": {}}), \
             mock.patch.object(to, "payloads_ready", return_value=True), \
             mock.patch.object(to, "pull_payload_manifest",
                               return_value={"path": "m", "payloads": 3}):
            to.plant_assume_breach(self.ctx, ssh_key="/k", runner=self.runner)
        to.bring_up_listeners.assert_called_once()


class DeployImplantsTests(unittest.TestCase):
    """The event-start half: seed the frozen matrix, prove it, and wipe the traces it made."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.comp = compfile(self.tmp.name, "red.assume_breach full\nred.c2_engine mythic\n"
                                          "red.clef_url http://10.0.0.9:8080\n")
        manifest = self.comp / "red" / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(
            {"boxes": {"192.168.101.2": {"stages": ["access", "channel"]}}}))

    def _patch(self):
        for name, kw in (("seed_via_red01", {"return_value": {"ok": True}}),
                         ("pull_manifest", {"return_value": {
                             "path": str(self.comp / "red" / "manifest.json")}}),
                         ("_replay_account_modes", {}),
                         ("_wipe_arming_traces", {})):
            patcher = mock.patch.object(to, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(gaia_ops, "mythic_enabled", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_seeds_and_wipes_the_traces_the_seed_made(self):
        self._patch()
        out = to.deploy_implants(self.comp, {"red": {"red_ip": "10.0.0.241"}},
                                 ssh_key="/k")
        to.seed_via_red01.assert_called_once()
        to._wipe_arming_traces.assert_called_once()
        self.assertEqual(out["red_ip"], "10.0.0.241")

    def test_the_account_modes_are_replayed_after_the_seed(self):
        # The seed's chpasswd rewrites /etc/shadow and undoes the packet's mode pins, so
        # the late pass is one more account writer between the seed and the wipe.
        self._patch()
        order = []
        to.seed_via_red01.side_effect = lambda *a, **k: order.append("seed")
        to._replay_account_modes.side_effect = lambda *a, **k: order.append("modes")
        to._wipe_arming_traces.side_effect = lambda *a, **k: order.append("wipe")
        to.deploy_implants(self.comp, {"red": {"red_ip": "10.0.0.241"}}, ssh_key="/k")
        self.assertEqual(order, ["seed", "modes", "wipe"])

    def test_a_range_that_was_never_staged_refuses(self):
        self._patch()
        with self.assertRaises(RuntimeError) as cm:
            to.deploy_implants(self.comp, {}, ssh_key="/k")
        self.assertIn("never staged", str(cm.exception))

    def test_assume_breach_off_has_nothing_to_deploy(self):
        off = compfile(self.tmp.name + "off", "red.assume_breach off\nred.c2 off\n"
                                            "red.clef_url http://clef.test\n")
        self._patch()
        self.assertIsNone(to.deploy_implants(off, {"red": {"red_ip": "10.0.0.241"}},
                                             ssh_key="/k"))
        to.seed_via_red01.assert_not_called()

    def _sliver_arm(self, comp, order):
        def note(key):
            def call(*_args, **_kwargs):
                order.append(key)
            return call
        (comp / "red").mkdir(parents=True, exist_ok=True)
        manifest = comp / "red" / "manifest.json"
        manifest.write_text(json.dumps(
            {"boxes": {"192.168.101.2": {"stages": ["access", "channel"]}}}))
        for name, key in (("install_red_rules", "rules"), ("bring_up_listeners", "listen"),
                          ("seed_via_red01", "seed")):
            patcher = mock.patch.object(to, name, side_effect=note(key))
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, kw in (("pull_manifest", {"return_value": {"path": str(manifest)}}),
                         ("_replay_account_modes", {}), ("_wipe_arming_traces", {})):
            patcher = mock.patch.object(to, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_sliver_arm_listens_on_red01_before_it_seeds(self):
        # Rules first (a rebooted engine drops them), then the listeners, then the payloads:
        # a callback that is expected before either exists is a failed channel gate.
        comp = compfile(self.tmp.name + "arm", "red.assume_breach full\n"
                                              "red.clef_url http://10.0.0.9:8080\n")
        order = []
        self._sliver_arm(comp, order)
        with mock.patch.object(to, "payloads_ready", return_value=True):
            to.deploy_implants(comp, {"red": {"red_ip": "10.0.0.241"},
                                      "teams": {"team1": {"identifier": "101"}}},
                               ssh_key="/k")
        self.assertEqual(order, ["rules", "listen", "seed"])

    def test_the_sliver_arm_refuses_a_range_with_no_frozen_matrix(self):
        # The arm never compiles: a range whose snapshots predate the freeze has nothing to
        # plant, and that has to be said plainly instead of failing 400 payloads later.
        comp = compfile(self.tmp.name + "none", "red.assume_breach full\n"
                                               "red.clef_url http://10.0.0.9:8080\n")
        with mock.patch.object(to, "install_red_rules"), \
             mock.patch.object(to, "payloads_ready", return_value=False), \
             mock.patch.object(to, "bring_up_listeners") as up:
            with self.assertRaises(RuntimeError) as cm:
                to.deploy_implants(comp, {"red": {"red_ip": "10.0.0.241"}}, ssh_key="/k")
        self.assertIn("no frozen payload matrix", str(cm.exception))
        up.assert_not_called()

    def test_the_listener_call_never_touches_the_frozen_matrix(self):
        # comp_prep_remote_command is the same job with a build behind it, and that build
        # starts by deleting /root/.sliver/slivers — mid-event, that is the payload store.
        listeners = to.listeners_command()
        prep = to.comp_prep_remote_command()
        self.assertIn("c2-prep --listeners-only", listeners)
        self.assertNotIn("rm -rf", listeners)
        self.assertIn("rm -rf", prep)


class PayloadManifestGuardTests(unittest.TestCase):
    """A C2-enabled stage must not silently ship an empty payload matrix."""

    def test_c2_enabled_with_no_manifest_names_comp_prep(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp)
            with self.assertRaises(RuntimeError) as cm:
                to.stage_red01("/k", "10.0.0.241", comp, c2=True)
        self.assertIn("comp-prep", str(cm.exception))
        self.assertIn("tlaloc c2-prep", str(cm.exception))

    def test_an_empty_manifest_is_not_a_staged_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp)
            payloads = comp / "red" / "payloads"
            payloads.mkdir(parents=True)
            (payloads / "manifest.json").write_text(json.dumps({"boxes": {}}))
            with self.assertRaises(RuntimeError) as cm:
                to.stage_red01("/k", "10.0.0.241", comp, c2=True)
        self.assertIn("lists no boxes", str(cm.exception))
        self.assertIn("comp-prep", str(cm.exception))

    def test_a_missing_referenced_payload_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp)
            payloads = comp / "red" / "payloads"
            payloads.mkdir(parents=True)
            (payloads / "manifest.json").write_text(json.dumps(
                {"boxes": {"web01-1:http": {"file": "web01-1-http.bin"}}}))
            with self.assertRaises(RuntimeError) as cm:
                to.stage_red01("/k", "10.0.0.241", comp, c2=True)
        self.assertIn("web01-1-http.bin", str(cm.exception))
        self.assertIn("comp-prep", str(cm.exception))

    def test_a_complete_manifest_is_accepted_and_staged(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp)
            payloads = comp / "red" / "payloads"
            payloads.mkdir(parents=True)
            (payloads / "web01-1-http.bin").write_bytes(b"\x7fELF")
            (payloads / "manifest.json").write_text(json.dumps(
                {"boxes": {"web01-1:http": {"file": "web01-1-http.bin"}}}))
            runner = mock.Mock(return_value=Completed(0))
            with mock.patch.object(to.subprocess, "run",
                                   return_value=Completed(0, b"")):
                out = to.stage_red01("/k", "10.0.0.241", comp, runner=runner,
                                     c2=True)
        self.assertTrue(out["ok"])
        self.assertTrue(runner.called)


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
