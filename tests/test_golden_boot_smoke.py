"""Golden boot smoke gate (2026-09-24 `systemd-system-masked` incident).

Before a golden is sealed into a template, `build_golden_set` boots ONE throwaway
full clone of its disk and requires multi-user over SSH (guest agent for Windows).
Anything other than a proof of boot is a failure: verified-unbootable and
could-not-verify both raise, so the gate can never pass by accident — the original
incident shipped because `strict=True` only saw rc=0 while the disk was bootless.

All Proxmox/SSH boundaries are mocked; no network, no VMs.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import golden_ops

TEAM = {"team1": {"identifier": 104}}
BOXES = [{"name": "web01", "template": "ubuntu-2204-web"}]
TEMPLATE_MAP = {"ubuntu-2204-web": 956}
SMOKE_VMID = 1900
SMOKE_NAME = "golden-web01-bootsmoke"


def _target(vmid=1150, ip="192.168.104.240"):
    return {"box": BOXES[0], "box_idx": 0, "vmid": vmid, "ip": ip,
            "vm_name": "golden-web01", "machine": "web01-golden",
            "gateway": "192.168.104.1", "bridge": "vmbr104"}


class FakePVE:
    """proxmox_api stub for the smoke path: nextid, clone, tags, node VM list."""

    def __init__(self, clone_error=None, listed_name=SMOKE_NAME, listed=True):
        self.clone_error = clone_error
        self.listed_name = listed_name
        self.listed = listed
        self.calls = []

    def __call__(self, method, path, **kw):
        self.calls.append((method, path, kw.get("data")))
        if method == "GET" and path == "/cluster/nextid":
            return {"data": str(SMOKE_VMID)}
        if method == "POST" and path.endswith("/clone"):
            if self.clone_error is not None:
                raise self.clone_error
            return {"data": "UPID:node:0000:clone"}
        if method == "GET" and path == "/nodes/node/qemu":
            if not self.listed:
                return {"data": []}
            return {"data": [{"vmid": SMOKE_VMID, "name": self.listed_name,
                              "status": "running"}]}
        return {"data": None}

    def clone_calls(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/clone")]


def _ssh(returncode=0, stdout="active\n", stderr=""):
    return MagicMock(return_value=SimpleNamespace(returncode=returncode, stdout=stdout,
                                                  stderr=stderr))


@contextmanager
def _stack(fake, ssh=None, start_error=None, guest_agent=None, is_win=False):
    with ExitStack() as stack:
        stack.enter_context(patch.object(golden_ops, "proxmox_api", fake))
        stack.enter_context(patch.object(golden_ops, "cluster_vms_for", return_value=[]))
        stack.enter_context(patch.object(golden_ops, "wait_for_proxmox_task"))
        stack.enter_context(patch.object(golden_ops, "gc_orphan_volumes"))
        start = stack.enter_context(
            patch.object(golden_ops, "start_vm", side_effect=start_error))
        destroy = stack.enter_context(patch.object(golden_ops, "destroy_vm_if_exists"))
        probe_ssh = ssh if ssh is not None else _ssh()
        stack.enter_context(patch.object(golden_ops, "ssh_via_gateway", probe_ssh))
        agent = guest_agent if guest_agent is not None else MagicMock(return_value=True)
        stack.enter_context(patch.object(golden_ops, "wait_for_guest_agent", agent))
        stack.enter_context(patch.object(golden_ops, "is_windows_template",
                                         return_value=is_win))
        yield SimpleNamespace(ssh=probe_ssh, start=start, destroy=destroy, agent=agent,
                              fake=fake)


class GoldenBootSmokeTests(unittest.TestCase):
    def test_pass_when_probe_reaches_multi_user(self):
        fake = FakePVE()
        with tempfile.TemporaryDirectory() as tmp, _stack(fake) as ctx:
            result = golden_ops.golden_boot_smoke("node", _target(), {}, Path(tmp))

        self.assertIs(result, True)
        # One FULL clone of the golden disk (linked clones require a template — qm(1)),
        # named uniquely so teardown can prove ownership by name.
        clones = fake.clone_calls()
        self.assertEqual(1, len(clones))
        self.assertEqual({"newid": SMOKE_VMID, "name": SMOKE_NAME, "full": 1},
                         {k: clones[0][2][k] for k in ("newid", "name", "full")})
        ctx.start.assert_called_once()
        # The probe asserts the exact thing the 2026-09-24 masked target broke.
        self.assertIn("multi-user.target", ctx.ssh.call_args.args[2])
        ctx.destroy.assert_called_once()
        self.assertEqual(SMOKE_VMID, ctx.destroy.call_args.args[1])

    def test_fails_when_ssh_up_but_multi_user_target_masked(self):
        # SSH answering while multi-user.target is inactive is the systemd-system-masked
        # signature — it must not be treated as a pass.
        fake = FakePVE()
        with tempfile.TemporaryDirectory() as tmp, _stack(
                fake, ssh=_ssh(stdout="inactive\n")) as ctx:
            with self.assertRaises(RuntimeError) as e:
                golden_ops.golden_boot_smoke("node", _target(), {}, Path(tmp),
                                             timeout=0.1, poll=0)
        msg = str(e.exception)
        self.assertIn(golden_ops.BOOT_SMOKE_UNBOOTABLE, msg)
        self.assertIn("web01", msg)
        self.assertIn("systemd-system-masked", msg)
        self.assertIn("FINAL_STAGE_CONFIGS", msg)
        ctx.destroy.assert_called_once()

    def test_fails_when_probe_never_answers(self):
        fake = FakePVE()
        ssh = _ssh(returncode=255, stdout="", stderr="Connection refused")
        with tempfile.TemporaryDirectory() as tmp, _stack(fake, ssh=ssh) as ctx:
            with self.assertRaises(RuntimeError) as e:
                golden_ops.golden_boot_smoke("node", _target(), {}, Path(tmp),
                                             timeout=0.1, poll=0)
        self.assertIn(golden_ops.BOOT_SMOKE_UNBOOTABLE, str(e.exception))
        self.assertGreater(ssh.call_count, 0)
        ctx.destroy.assert_called_once()

    def test_fails_and_tears_down_when_probe_errors_out(self):
        # A probe that cannot even run (bad ctx/key, no ssh binary) must fail closed,
        # fast — never spin for the full budget and never be read as bootable.
        fake = FakePVE()
        ssh = MagicMock(side_effect=FileNotFoundError("ssh: command not found"))
        with tempfile.TemporaryDirectory() as tmp, _stack(fake, ssh=ssh) as ctx:
            with self.assertRaises(RuntimeError) as e:
                golden_ops.golden_boot_smoke("node", _target(), {}, Path(tmp), timeout=60)
        msg = str(e.exception)
        self.assertIn(golden_ops.BOOT_SMOKE_UNVERIFIED, msg)
        self.assertIn("probe could not run", msg)
        ctx.destroy.assert_called_once()

    def test_fails_when_clone_cannot_be_created(self):
        fake = FakePVE(clone_error=RuntimeError("clone task failed: no space left"))
        with tempfile.TemporaryDirectory() as tmp, _stack(fake) as ctx:
            with self.assertRaises(RuntimeError) as e:
                golden_ops.golden_boot_smoke("node", _target(), {}, Path(tmp))
        msg = str(e.exception)
        self.assertIn("COULD NOT VERIFY", msg)
        self.assertIn("could not be created", msg)
        ctx.start.assert_not_called()

    def test_fails_when_clone_will_not_start(self):
        fake = FakePVE()
        with tempfile.TemporaryDirectory() as tmp, _stack(fake, start_error=RuntimeError("start failed")) as ctx:
            with self.assertRaises(RuntimeError) as e:
                golden_ops.golden_boot_smoke("node", _target(), {}, Path(tmp))
        self.assertIn("COULD NOT VERIFY", str(e.exception))
        ctx.destroy.assert_called_once()

    def test_destroy_leaves_a_foreign_vmid_alone(self):
        # destroy_vm_if_exists destroys untagged VMs on vmid math, so a vmid that raced
        # into a foreign owner's hands must never reach it.
        fake = FakePVE(listed_name="someone-elses-vm")
        with tempfile.TemporaryDirectory() as tmp, _stack(fake) as ctx:
            golden_ops._destroy_smoke_clone("node", SMOKE_VMID, SMOKE_NAME, Path(tmp))
        ctx.destroy.assert_not_called()

    def test_destroy_gcs_volumes_when_no_vm_exists(self):
        fake = FakePVE(listed=False)
        with tempfile.TemporaryDirectory() as tmp, _stack(fake) as ctx:
            with patch.object(golden_ops, "gc_orphan_volumes") as gc:
                golden_ops._destroy_smoke_clone("node", SMOKE_VMID, SMOKE_NAME, Path(tmp))
        ctx.destroy.assert_not_called()
        gc.assert_called_once()

    def test_windows_probe_uses_guest_agent(self):
        fake = FakePVE()
        agent = MagicMock(return_value=True)
        with tempfile.TemporaryDirectory() as tmp, _stack(fake, is_win=True,
                                                          guest_agent=agent) as ctx:
            result = golden_ops.golden_boot_smoke("node", _target(), {}, Path(tmp))
        self.assertIs(result, True)
        agent.assert_called_once()
        ctx.ssh.assert_not_called()

    def test_windows_probe_failure_is_unbootable(self):
        fake = FakePVE()
        agent = MagicMock(return_value=False)
        with tempfile.TemporaryDirectory() as tmp, _stack(fake, is_win=True,
                                                          guest_agent=agent) as ctx:
            with self.assertRaises(RuntimeError) as e:
                golden_ops.golden_boot_smoke("node", _target(), {}, Path(tmp))
        self.assertIn(golden_ops.BOOT_SMOKE_UNBOOTABLE, str(e.exception))
        ctx.destroy.assert_called_once()

    def test_boot_hostile_planted_config_is_named_in_the_error(self):
        fake = FakePVE()
        with tempfile.TemporaryDirectory() as tmp, _stack(
                fake, ssh=_ssh(stdout="inactive\n")) as ctx:
            with self.assertRaises(RuntimeError) as e:
                golden_ops.golden_boot_smoke(
                    "node", _target(), {}, Path(tmp), timeout=0.1, poll=0,
                    planted_configs=["install-nginx", "systemd-system-masked"])
        self.assertIn("systemd-system-masked", str(e.exception))
        ctx.destroy.assert_called_once()

    def test_fails_when_no_vmid_can_be_allocated(self):
        with tempfile.TemporaryDirectory() as tmp, _stack(FakePVE()) as ctx:
            with patch.object(golden_ops, "cluster_vms_for",
                              side_effect=RuntimeError("API down")):
                with self.assertRaises(RuntimeError) as e:
                    golden_ops.golden_boot_smoke("node", _target(), {}, Path(tmp))
        self.assertIn("COULD NOT VERIFY", str(e.exception))
        self.assertIn("no vmid", str(e.exception))
        ctx.destroy.assert_not_called()

    def test_smoke_vmid_skips_an_id_already_on_the_node(self):
        # Multi-node: /cluster/nextid can be answered by the primary while this golden's
        # satellite already owns that id.
        fake = FakePVE()
        with patch.object(golden_ops, "proxmox_api", fake), \
             patch.object(golden_ops, "cluster_vms_for",
                          return_value=[{"vmid": SMOKE_VMID}]):
            self.assertEqual(SMOKE_VMID + 1, golden_ops._smoke_vmid("node"))

    def test_smoke_vmid_falls_back_when_pool_allocator_is_unavailable(self):
        with patch.object(golden_ops, "proxmox_api",
                          side_effect=RuntimeError("API down")), \
             patch.object(golden_ops, "cluster_vms_for", return_value=[{"vmid": 500}]):
            self.assertEqual(501, golden_ops._smoke_vmid("node"))


class BuildGoldenSetBootSmokeTests(unittest.TestCase):
    """The gate's integration point: stop -> smoke -> convert, with `golden_boot_smoke`
    as the only off switch (and a loud warning when it is off)."""

    def _build(self, compfile_text, smoke, boxes=BOXES, unbooted=frozenset(),
               template_map=TEMPLATE_MAP, configurations=()):
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            if compfile_text is not None:
                (comp_dir / "Compfile").write_text(compfile_text)
            golden_cfg = comp_dir / "nakon-golden.json"
            # No plantable configurations by default: the strict nakon plant is skipped,
            # so these tests exercise the tail (stop -> smoke -> convert) in isolation.
            # The smoke still runs — a pristine golden is verified too, just never skipped.
            golden_cfg.write_text(json.dumps({"machines": [
                {"name": f"{boxes[0]['name']}-golden", "ip": "192.168.104.240",
                 "configurations": list(configurations)}]}))
            with ExitStack() as stack:
                stack.enter_context(patch.dict(
                    os.environ, {"TF_VAR_ssh_public_key": "ssh-rsa AAAA test"}))
                p_api = stack.enter_context(patch.object(golden_ops, "proxmox_api"))
                stack.enter_context(patch.object(golden_ops, "_vm_exists",
                                                 return_value=False))
                stack.enter_context(patch.object(golden_ops, "_is_template",
                                                 return_value=False))
                stack.enter_context(patch.object(golden_ops, "_template_vmid_map",
                                                 return_value=template_map))
                stack.enter_context(patch.object(golden_ops, "wait_for_proxmox_task"))
                # gc_orphan_volumes lives in range_ops and reaches the real API through
                # range_ops' own globals — stub it rather than the whole env.
                stack.enter_context(patch.object(golden_ops, "gc_orphan_volumes"))
                stack.enter_context(patch.object(golden_ops, "destroy_vm_if_exists"))
                stack.enter_context(patch.object(golden_ops, "start_vm"))
                p_stop = stack.enter_context(patch.object(golden_ops, "stop_vm"))
                stack.enter_context(patch.object(golden_ops, "take_snapshot"))
                stack.enter_context(patch.object(golden_ops, "list_snapshots",
                                                 return_value=set()))
                stack.enter_context(patch.object(golden_ops, "delete_snapshot"))
                stack.enter_context(patch.object(golden_ops, "prep_apt_on_boxes"))
                stack.enter_context(patch.object(golden_ops, "setup_ubuntu_auth"))
                stack.enter_context(patch.object(golden_ops, "expand_guest_root_disks"))
                stack.enter_context(patch.object(golden_ops, "fix_dns_on_boxes"))
                stack.enter_context(patch.object(golden_ops, "ensure_nat_forwarding"))
                stack.enter_context(patch.object(golden_ops, "build_nakon_bundle"))
                stack.enter_context(patch.object(
                    golden_ops, "run_nakon",
                    return_value=SimpleNamespace(failed=[])))
                stack.enter_context(patch.object(
                    golden_ops, "ssh_via_gateway",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr="")))
                stack.enter_context(patch.object(golden_ops, "wait_for_boxes_ssh"))
                stack.enter_context(patch.object(golden_ops, "wait_for_cloud_init"))
                stack.enter_context(patch.object(golden_ops, "golden_boot_smoke", smoke))
                out = io.StringIO()
                error = None
                result = None
                try:
                    with redirect_stdout(out):
                        result = golden_ops.build_golden_set(
                            "node", TEAM, boxes, {}, comp_dir, 1000, "pw",
                            golden_cfg, None, "ops", "10.0.0.9", unbooted=unbooted)
                except RuntimeError as e:
                    error = e
        calls = [(c.args[0], c.args[1]) for c in p_api.call_args_list]
        return SimpleNamespace(result=result, error=error, out=out.getvalue(),
                               calls=calls, stop=p_stop, smoke=smoke)

    def test_smoke_runs_before_conversion_and_template_is_made(self):
        smoke = MagicMock(return_value=True)
        r = self._build(None, smoke)
        self.assertIsNone(r.error)
        self.assertEqual({"web01": 1150}, r.result)
        smoke.assert_called_once()
        self.assertIn(("POST", "/nodes/node/qemu/1150/template"), r.calls)

    def test_failed_smoke_blocks_template_conversion(self):
        smoke = MagicMock(side_effect=RuntimeError(
            "golden boot smoke FAILED (verified-unbootable) for box type 'web01'"))
        r = self._build(None, smoke)
        self.assertIsNotNone(r.error)
        smoke.assert_called_once()
        r.stop.assert_called_once()
        self.assertNotIn(("POST", "/nodes/node/qemu/1150/template"), r.calls)

    def test_compfile_knob_disables_check_and_warns(self):
        smoke = MagicMock(return_value=True)
        r = self._build("golden_boot_smoke 0\nalpine_services 0\n", smoke)
        self.assertIsNone(r.error)
        smoke.assert_not_called()
        self.assertIn("WARNING", r.out)
        self.assertIn("UNVERIFIED", r.out)
        self.assertIn(("POST", "/nodes/node/qemu/1150/template"), r.calls)

    def test_golden_stage_configs_are_handed_to_the_smoke(self):
        # The failure message can then name the culprit when the golden-stage plan
        # already carries a FINAL_STAGE_CONFIGS entry.
        smoke = MagicMock(return_value=True)
        r = self._build(None, smoke, configurations=["install-nginx",
                                                     "systemd-system-masked"])
        self.assertIsNone(r.error)
        self.assertEqual(["install-nginx", "systemd-system-masked"],
                         smoke.call_args.kwargs["planted_configs"])

    def test_unbooted_dc_golden_is_never_booted_or_smoked(self):
        # Booting a DC golden would defeat the unbooted split (duplicate DomainSID) —
        # the cold path must short-circuit before the smoke gate.
        dc = [{"name": "dc01", "template": "windows-server-2022"}]
        smoke = MagicMock(return_value=True)
        r = self._build(None, smoke, boxes=dc, unbooted={"dc01"},
                        template_map={"windows-server-2022": 955})
        self.assertIsNone(r.error)
        self.assertEqual({"dc01": 1150}, r.result)
        smoke.assert_not_called()
        r.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
