"""A machine with no pins (empty `configurations`) is a legal lineup entry.

nakon's runner used to fail its 0-step plan as "no output from the remote plan — check
credentials" (live-found 2026-10-04); fixed upstream in vendor/nakon (zero-step-plan), and the
driver-side reconcile that carried us is gone. These tests pin the contract on both sides:
the driver hands a zero-pin machine to nakon untouched, and nakon's runner records a silent
clean exit of a 0-step plan as success while still failing a plan that HAD steps and never
reported."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "vendor" / "nakon"))

import nakon_ops  # noqa: E402
from nakon.deploy import runner  # noqa: E402


class DriverHasNoWorkaround(unittest.TestCase):
    def test_reconcile_symptom_patch_is_gone(self):
        self.assertFalse(hasattr(nakon_ops, "_reconcile_zero_step_machines"))
        import nakon_run_ops
        self.assertFalse(hasattr(nakon_run_ops, "_reconcile_zero_step_machines"))

    def test_clean_zero_step_outcome_passes_through_untouched(self):
        """run_nakon returns nakon's per-machine JSON verbatim: an empty-step, error-free
        machine is a clean outcome the nothing-answered floor accepts alongside a ran one."""
        machines = [{"name": "db01", "error": None, "steps": []},
                    {"name": "web01", "error": None, "steps": [{"name": "x", "rc": 0}]}]
        self.assertIsNone(nakon_ops._nothing_answered(machines))
        self.assertEqual(machines[0], {"name": "db01", "error": None, "steps": []})


def _run(step_count, report_lines=()):
    """Drive runner.deploy_machine with ssh faked: the remote exits 0 and prints
    `report_lines` between the report markers (none = silent)."""
    plan_entry = {"steps": [{}] * step_count}
    bundle = MagicMock()
    bundle.plan_for.return_value = ("p" * 64, plan_entry)
    bundle.archive_path.return_value = Path(tempfile.gettempdir()) / "tz-zero-step.tar"
    bundle.archive_path.return_value.write_bytes(b"x")
    bundle.inventory_entry.return_value = None
    machine = {"name": "db01", "ip": "10.0.0.5", "platform": "linux", "password": "pw"}

    def fake_stream(client, command, password, on_line, on_idle=None):
        for line in report_lines:
            on_line(line)
        return 0

    with patch.object(runner.ssh, "connect", return_value=MagicMock()), \
            patch.object(runner.ssh, "put_file"), \
            patch.object(runner.ssh, "run_streaming", side_effect=fake_stream), \
            patch.object(runner.ssh, "force_cleanup"), \
            patch.object(runner.ssh, "render_bootstrap_sh", return_value="#!/bin/sh\n"):
        return runner.deploy_machine(bundle, machine, emit=lambda _l: None)


class NakonZeroStepBranch(unittest.TestCase):
    def test_zero_step_silent_clean_exit_is_success(self):
        outcome = _run(step_count=0)
        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.progress.results(), [])

    def test_plan_with_steps_that_never_reported_still_fails(self):
        outcome = _run(step_count=3)
        self.assertIn("no output from the remote plan", outcome.error)


if __name__ == "__main__":
    unittest.main()
