"""The engine-template build VM's identity stamp.

The template clean (`clean_engine_for_template`) is the most destructive command in the
pipeline: it drops the containers and volumes, `.env`, `event.conf`, machine-id, cloud-init
state and the SSH host keys. It runs over SSH to a management IP that a second
competition's live engine can be sharing, and with two VMs on one address ARP flaps can
deliver it to the wrong machine. Observed (known-issues): an engine lost
`/etc/ssh/ssh_host_*` — every session reset at kex while the listener stayed up — plus its
`/opt/quotient/.env` and containers.

The fix is a stamp written when the build VM first accepts SSH, verified before anything
destructive, and removed by the clean so it is never baked into the template.

Offline; `_run_engine_cmd` is faked.
"""

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import engine_ops  # noqa: E402


def _ctx(ip="10.0.0.250"):
    return {"ssh_key_path": "/k", "vm_username": "ubuntu", "scoring_engine_ip": ip}


class _CmdRecorder:
    """Stands in for _run_engine_cmd: records calls, answers the identity probe."""

    def __init__(self, stamp=""):
        self.stamp = stamp
        self.calls = []

    def __call__(self, ctx, cmd, check=True, timeout=60, capture=False, step=None):
        self.calls.append({"cmd": cmd, "step": step, "capture": capture})
        if "cat " in cmd and engine_ops.ENGINE_BUILD_STAMP in cmd:
            return types.SimpleNamespace(returncode=0, stdout=self.stamp, stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def destructive(self):
        return [c for c in self.calls if "docker compose down" in c["cmd"]]

    def probed(self):
        return [c for c in self.calls
                if "cat " in c["cmd"] and engine_ops.ENGINE_BUILD_STAMP in c["cmd"]]


class IdentityProbe(unittest.TestCase):
    def test_matching_stamp_passes(self):
        rec = _CmdRecorder(stamp=engine_ops.engine_build_identity(1240) + "\n")
        with patch.object(engine_ops, "_run_engine_cmd", rec):
            engine_ops.assert_engine_build_identity(_ctx(), 1240)   # no raise

    def test_a_foreign_engine_is_refused(self):
        """No stamp = a machine this pipeline never stamped: a foreign engine, or a clone
        made from a template. Both are worse than stopping."""
        rec = _CmdRecorder(stamp="")
        with patch.object(engine_ops, "_run_engine_cmd", rec):
            with self.assertRaises(RuntimeError) as ctx:
                engine_ops.assert_engine_build_identity(_ctx(), 1240)
        message = str(ctx.exception)
        self.assertIn("refusing", message)
        self.assertIn("10.0.0.250", message)                  # names the address
        self.assertIn("Nothing was changed", message)         # reassures the operator
        self.assertIn("TF_VAR_engine_mgmt_ip", message)       # names the real remedy

    def test_another_slots_build_is_refused(self):
        rec = _CmdRecorder(stamp=engine_ops.engine_build_identity(1140) + "\n")
        with patch.object(engine_ops, "_run_engine_cmd", rec):
            with self.assertRaises(RuntimeError) as ctx:
                engine_ops.assert_engine_build_identity(_ctx(), 1240)
        self.assertIn("tezcatlipoca-engine-template/1140", str(ctx.exception))

    def test_the_identity_is_keyed_on_the_reserved_vmid(self):
        self.assertNotEqual(engine_ops.engine_build_identity(1240),
                            engine_ops.engine_build_identity(1340))


class CleanVerifiesBeforeDestroying(unittest.TestCase):
    def test_a_clean_with_the_vmid_probes_before_it_destroys(self):
        rec = _CmdRecorder(stamp="")
        with patch.object(engine_ops, "_run_engine_cmd", rec):
            with self.assertRaises(RuntimeError):
                engine_ops.clean_engine_for_template(_ctx(), 1240)
        self.assertTrue(rec.probed(), "the identity must be probed")
        self.assertEqual(rec.destructive(), [],
                         "the destructive command must NOT run when the identity is wrong")

    def test_a_clean_on_the_right_machine_does_destroy(self):
        rec = _CmdRecorder(stamp=engine_ops.engine_build_identity(1240) + "\n")
        with patch.object(engine_ops, "_run_engine_cmd", rec):
            engine_ops.clean_engine_for_template(_ctx(), 1240)
        self.assertEqual(len(rec.destructive()), 1)
        # Probe first, destroy second — order is the whole point.
        self.assertLess(rec.calls.index(rec.probed()[0]),
                        rec.calls.index(rec.destructive()[0]))

    def test_the_stamp_is_removed_so_clones_never_carry_it(self):
        rec = _CmdRecorder(stamp=engine_ops.engine_build_identity(1240) + "\n")
        with patch.object(engine_ops, "_run_engine_cmd", rec):
            engine_ops.clean_engine_for_template(_ctx(), 1240)
        destroy_cmd = rec.destructive()[0]["cmd"]
        self.assertIn(f"rm -f {engine_ops.ENGINE_BUILD_STAMP}", destroy_cmd)

    def test_a_clean_without_a_vmid_stays_backward_compatible(self):
        """The shim at template_ops._clean_func() and any older caller pass only ctx."""
        rec = _CmdRecorder()
        with patch.object(engine_ops, "_run_engine_cmd", rec):
            engine_ops.clean_engine_for_template(_ctx())
        self.assertEqual(rec.probed(), [])
        self.assertEqual(len(rec.destructive()), 1)


class Stamping(unittest.TestCase):
    def test_stamping_writes_the_identity_with_sudo(self):
        rec = _CmdRecorder()
        with patch.object(engine_ops, "_run_engine_cmd", rec):
            engine_ops.stamp_engine_build(_ctx(), 1240)
        cmd = rec.calls[0]["cmd"]
        self.assertIn(engine_ops.engine_build_identity(1240), cmd)
        self.assertIn(f"tee {engine_ops.ENGINE_BUILD_STAMP}", cmd)
        self.assertIn("sudo", cmd)


if __name__ == "__main__":
    unittest.main()
