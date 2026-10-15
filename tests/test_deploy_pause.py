"""A pause request holds a deploy at a phase boundary until the request file is deleted."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from _deploy_patch import dpatch  # noqa: E402
from deploy_lib import pause, runner  # noqa: E402


class StubCtx:
    """The sequencer's whole view of a DeployContext: state, checkpoints, the phase list."""

    def __init__(self, comp_dir, from_phase=1, state=None):
        self.comp_dir = comp_dir
        self.comp_name = comp_dir.name
        self.from_phase = from_phase
        self.state = dict(state or {})
        self.state_path = comp_dir / ".deploy_state.json"

    def save_state(self):
        self.state_path.write_text(json.dumps(self.state))

    def checkpoint(self, n):
        self.state["last_phase"] = n
        self.save_state()


def _stub_phases(calls):
    def make(n):
        def phase(ctx):
            calls.append(("phase", n))
        return phase
    return tuple(make(n) for n in range(1, 9))


def _on_disk(ctx):
    try:
        return json.loads(ctx.state_path.read_text())
    except (OSError, ValueError):
        return {}


class PauseAtBoundary(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.comp_dir = Path(tmp.name)
        self.request = self.comp_dir / "pause-request"
        self._env = os.environ.get("TEZ_PAUSE_FILE")
        os.environ["TEZ_PAUSE_FILE"] = str(self.request)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("TEZ_PAUSE_FILE", None)
        else:
            os.environ["TEZ_PAUSE_FILE"] = self._env

    def _held(self, phases, ctx, body):
        """Run the pipeline on a thread under the stub phases and always join it before the
        patches come off: a parked thread left alive would wake up on the REAL phases."""
        with dpatch("PHASES", tuple(phases)), dpatch("connect_terraform"):
            thread = threading.Thread(target=runner.run_pipeline, args=(ctx,))
            thread.start()
            try:
                body()
            finally:
                self.request.unlink(missing_ok=True)
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "the parked pipeline never resumed")

    def _wait_for_hold(self, ctx, phase, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _on_disk(ctx).get("paused_after_phase") == phase:
                return
            time.sleep(0.01)
        self.fail(f"phase {phase} never recorded a hold on disk: {_on_disk(ctx)}")

    def test_a_request_made_during_a_phase_holds_at_that_phase_s_end(self):
        calls = []
        phases = list(_stub_phases(calls))
        ran_at_hold = []

        def phase2(ctx):
            calls.append(("phase", 2))
            self.request.touch()          # the operator asks while phase 2 is running

        phases[1] = phase2
        ctx = StubCtx(self.comp_dir)

        def body():
            self._wait_for_hold(ctx, 2)
            ran_at_hold.append([c[1] for c in calls if c[0] == "phase"])

        self._held(phases, ctx, body)
        self.assertEqual(ran_at_hold, [[1, 2]])          # nothing past the boundary ran
        self.assertEqual([c[1] for c in calls if c[0] == "phase"], list(range(1, 9)))
        self.assertNotIn("paused_after_phase", ctx.state)
        self.assertNotIn("paused_after_phase", _on_disk(ctx))

    def test_a_request_that_precedes_the_first_boundary_holds_after_phase_1(self):
        self.request.touch()                              # the run was queued when it was asked
        calls = []
        ctx = StubCtx(self.comp_dir)
        ran_at_hold = []

        def body():
            self._wait_for_hold(ctx, 1)
            ran_at_hold.append([c[1] for c in calls if c[0] == "phase"])

        self._held(_stub_phases(calls), ctx, body)
        self.assertEqual(ran_at_hold, [[1]])
        self.assertEqual([c[1] for c in calls if c[0] == "phase"], list(range(1, 9)))

    def test_a_skipped_phase_is_not_a_boundary(self):
        self.request.touch()
        calls = []
        ctx = StubCtx(self.comp_dir, from_phase=5, state={"last_phase": 4})
        ran_at_hold = []

        def body():
            self._wait_for_hold(ctx, 5)
            ran_at_hold.append([c[1] for c in calls if c[0] == "phase"])

        self._held(_stub_phases(calls), ctx, body)
        self.assertEqual(ran_at_hold, [[1, 2, 3, 4, 5]])

    def test_no_request_file_is_a_no_op(self):
        calls = []
        ctx = StubCtx(self.comp_dir)
        with dpatch("PHASES", _stub_phases(calls)), dpatch("connect_terraform"):
            runner.run_pipeline(ctx)
        self.assertEqual([c[1] for c in calls], list(range(1, 9)))
        self.assertNotIn("paused_after_phase", ctx.state)

    def test_an_unset_control_path_is_a_no_op_for_manual_deploys(self):
        os.environ.pop("TEZ_PAUSE_FILE", None)
        self.request.touch()                              # a stray file must not hold a hand-run
        ctx = StubCtx(self.comp_dir)
        pause.pause_at_boundary(ctx, 3)
        self.assertNotIn("paused_after_phase", ctx.state)

    def test_a_marker_left_by_a_run_that_died_while_parked_does_not_label_the_resume(self):
        calls = []
        ctx = StubCtx(self.comp_dir, from_phase=4, state={"last_phase": 3,
                                                          "paused_after_phase": 3})
        with dpatch("PHASES", _stub_phases(calls)), dpatch("connect_terraform"):
            runner.run_pipeline(ctx)
        self.assertNotIn("paused_after_phase", ctx.state)
        self.assertNotIn("paused_after_phase", _on_disk(ctx))


if __name__ == "__main__":
    unittest.main()
