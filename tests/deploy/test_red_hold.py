"""A4 — a held range sleeps; nothing attacks before the event.

run-14ac7f14 deployed --hold-start at 17:06 and tlaloc began cycling immediately,
uninstalling the Universal Forwarders provisioned minutes earlier. Now: phase 7 leaves
the loop HELD (marker in state), competition-ops start releases it, and scrim's
telemetry.ensure.sh hook runs only while the red director is stopped."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from deploy_lib.phases import final  # noqa: E402
from quotient import lifecycle_ops  # noqa: E402
from scrim import telemetry_ensure  # noqa: E402


def _ctx(tmp, hold):
    state = {"hold_start": hold} if hold else {}
    return SimpleNamespace(state=state, save_state=lambda: None,
                           comp_dir=Path(tmp), ssh_key="/k")


class HoldKeepsRedSleeping(unittest.TestCase):
    def test_held_deploy_defers_the_loop_and_marks_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, hold=True)
            with mock.patch("red.tlaloc_ops.start_director") as sd:
                final.begin_red_after_delivery(ctx, {"red_ip": "10.0.0.242"})
            sd.assert_not_called()
            self.assertTrue(ctx.state["red_loop_held"])

    def test_unheld_deploy_starts_the_loop_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, hold=False)
            with mock.patch("red.tlaloc_ops.start_director") as sd:
                final.begin_red_after_delivery(ctx, {"red_ip": "10.0.0.242"})
            sd.assert_called_once_with("/k", "10.0.0.242")
            self.assertNotIn("red_loop_held", ctx.state)

    def test_no_red_summary_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, hold=True)
            final.begin_red_after_delivery(ctx, None)
            self.assertNotIn("red_loop_held", ctx.state)


class ReleaseOnStart(unittest.TestCase):
    def test_release_starts_director_and_clears_marker(self):
        state = {"red_loop_held": True, "red": {"red_ip": "10.0.0.242"}}
        with mock.patch("red.tlaloc_ops.start_director") as sd, \
             mock.patch("estate.config.ssh_key_path", return_value="/k"):
            rc = lifecycle_ops.release_held_red(state)
        self.assertEqual(rc, 0)
        sd.assert_called_once_with("/k", "10.0.0.242")
        self.assertFalse(state["red_loop_held"])

    def test_no_marker_is_a_no_op(self):
        state = {}
        with mock.patch("red.tlaloc_ops.start_director") as sd:
            self.assertEqual(lifecycle_ops.release_held_red(state), 0)
        sd.assert_not_called()

    def test_failure_to_release_returns_rc1_for_the_operator(self):
        state = {"red_loop_held": True}          # no red_ip — can't dial
        with mock.patch("red.tlaloc_ops.start_director"):
            rc = lifecycle_ops.release_held_red(state)
        self.assertEqual(rc, 1)
        self.assertTrue(state["red_loop_held"])  # marker stays: fix state and re-start


class EnsureHook(unittest.TestCase):
    def _args(self, tmp):
        return SimpleNamespace(run_dir=str(tmp))

    def test_undeclared_comp_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(telemetry_ensure.run_if_declared(self._args(tmp), Path(tmp)))

    def test_declared_script_runs_with_comp_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = Path(tmp)
            (comp / "telemetry.ensure.sh").write_text("#!/bin/bash\ntrue\n")
            seen = {}

            def fake_run(cmd, cwd=None, env=None, timeout=None, check=True, tail=None):
                seen["cmd"], seen["env"] = cmd, env
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            with mock.patch("scrim.telemetry_ensure.procs.run", fake_run):
                telemetry_ensure.run_if_declared(self._args(tmp), comp)
            self.assertEqual(seen["env"]["TEZ_COMP_DIR"], str(comp))
            self.assertEqual(seen["cmd"][0], "bash")

    def test_failing_script_refuses_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = Path(tmp)
            (comp / "telemetry.ensure.sh").write_text("#!/bin/bash\nfalse\n")
            with mock.patch("scrim.telemetry_ensure.procs.run",
                            return_value=SimpleNamespace(returncode=3, stdout="", stderr="boom")):
                with self.assertRaises(RuntimeError):
                    telemetry_ensure.run_if_declared(self._args(tmp), comp)


if __name__ == "__main__":
    unittest.main()
