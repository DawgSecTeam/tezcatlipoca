"""Detached long-run helper.

Deploys take hours; tool calls and harness background tasks do not. The observed
compensation was worse than the problem — agents wrapped deploys in their own `timeout`,
converting a slow success into an abrupt partial-state kill (six exec logs carry a
leading `Terminated`). spawn_detached is the shape that survives: its own session, its
own log, and the caller polls.
"""

import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import utils  # noqa: E402


class SpawnDetached(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "run.log"
        self.pids = []

    def tearDown(self):
        for pid in self.pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass

    def _spawn(self, cmd):
        pid, log = utils.spawn_detached(cmd, self.log)
        self.pids.append(pid)
        return pid, log

    def test_it_returns_the_pid_and_the_log_path(self):
        pid, log = self._spawn("sleep 5")
        self.assertIsInstance(pid, int)
        self.assertEqual(log, str(self.log))

    def test_the_child_leads_its_own_session(self):
        """That is what makes it survive the caller and what makes a group kill work."""
        pid, _ = self._spawn("sleep 5")
        time.sleep(0.3)
        self.assertEqual(os.getpgid(pid), pid)

    def test_output_lands_in_the_log(self):
        self._spawn("echo hello-detached")
        for _ in range(50):
            if self.log.exists() and "hello-detached" in self.log.read_text():
                break
            time.sleep(0.05)
        self.assertIn("hello-detached", self.log.read_text())

    def test_the_thread_returns_before_the_command_finishes(self):
        start = time.time()
        self._spawn("sleep 2")
        self.assertLess(time.time() - start, 1.0, "spawn_detached must not block")

    def test_a_missing_log_directory_is_created(self):
        nested = Path(self.tmp.name) / "logs" / "deeper" / "run.log"
        pid, log = utils.spawn_detached("echo nested", nested)
        self.pids.append(pid)
        for _ in range(50):
            if nested.exists() and "nested" in nested.read_text():
                break
            time.sleep(0.05)
        self.assertIn("nested", nested.read_text())

    def test_a_group_kill_takes_the_whole_tree(self):
        pid, _ = self._spawn("sleep 30")
        time.sleep(0.3)
        os.killpg(pid, signal.SIGTERM)
        for _ in range(50):
            try:
                state = open(f"/proc/{pid}/stat").read().split(") ", 1)[1].split()[0]
            except FileNotFoundError:
                state = "gone"
            if state in ("Z", "gone"):
                break
            time.sleep(0.05)
        self.assertIn(state, ("Z", "gone"), "the process group survived SIGTERM")

    def test_a_list_command_is_run_without_a_shell(self):
        pid, _ = utils.spawn_detached(["/bin/echo", "from-argv"], self.log)
        self.pids.append(pid)
        for _ in range(50):
            if self.log.exists() and "from-argv" in self.log.read_text():
                break
            time.sleep(0.05)
        self.assertIn("from-argv", self.log.read_text())

    def test_the_log_is_appended_not_truncated(self):
        self.log.write_text("earlier run\n")
        self._spawn("echo later run")
        for _ in range(50):
            if "later run" in self.log.read_text():
                break
            time.sleep(0.05)
        text = self.log.read_text()
        self.assertIn("earlier run", text)
        self.assertIn("later run", text)


if __name__ == "__main__":
    unittest.main()
