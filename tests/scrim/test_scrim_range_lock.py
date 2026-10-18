"""One scrim per resolved range workspace.

Two scrims of one range ran concurrently once: both stopped nginx for the fire test, both
logged in as the same Quotient
accounts, and the second bad-auto deploy reconfigured the first's live red01. The engine
flock does not cover a --skip-deploy scrim, so the range's own workspace takes a flock.
"""

import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scrim import lock_ops


class ScrimRangeLock(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = mock.patch.dict(os.environ, {"HOME": tmp.name})
        home.start()
        self.addCleanup(home.stop)
        self.comp = Path(tmp.name) / "competitions" / "scrim-one-light"
        self.comp.mkdir(parents=True)

    def tearDown(self):
        for fh in lock_ops._LOCKS.values():
            fh.close()
        lock_ops._LOCKS.clear()

    def test_a_second_scrim_of_the_range_is_refused_with_the_holder_named(self):
        path = lock_ops.scrim_lock_path(self.comp)
        path.parent.mkdir(parents=True, exist_ok=True)
        holder = open(path, "a+")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        holder.write("pid 4242 — run-agent-scrim.py")
        holder.flush()

        with self.assertRaises(SystemExit) as raised:
            lock_ops.acquire_scrim_lock(self.comp)
        msg = str(raised.exception)
        self.assertIn("pid 4242", msg)
        self.assertIn(path.name, msg)
        self.assertIn("already running", msg)
        # the refusal read the holder's line back, so the open before the flock must not
        # truncate it (the engine lock's "w" mode would have wiped it)
        self.assertIn("pid 4242", path.read_text())

    def test_acquire_is_idempotent_per_process(self):
        lock_ops.acquire_scrim_lock(self.comp)
        lock_ops.acquire_scrim_lock(self.comp)          # second call is a no-op
        self.assertIn(f"pid {os.getpid()}",
                      lock_ops.scrim_lock_path(self.comp).read_text())

    def test_a_different_workspace_takes_its_own_lock(self):
        other = self.comp.parent / "amongus-cde"
        other.mkdir()
        lock_ops.acquire_scrim_lock(self.comp)
        lock_ops.acquire_scrim_lock(other)
        self.assertNotEqual(lock_ops.scrim_lock_path(self.comp),
                            lock_ops.scrim_lock_path(other))


if __name__ == "__main__":
    unittest.main()
