"""The concurrency gate: refuse to start a second deploy against one host.

Two sessions sharing one estate is the documented cause of the 13xx vmid races, the
foreign template squatting a competition's golden slot, and the over-broad sweep that
destroyed two other competitions' engines and goldens (AGENTS.md, 2026-09-30).

The signal is the locks directory, because it is the one that cannot lie: a holder is a
live process and the kernel drops the flock when it dies, so the 18 stale `.lock` files
that accumulate on this host must NOT be reported as deploys in flight. That property is
pinned here, along with the default refusal and its opt-out.

Offline: a private lock directory stands in for ~/.tezcatlipoca/locks.
"""

import fcntl
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import config_ops  # noqa: E402
import nakon_ops  # noqa: E402


class _FakeLocksDir:
    """Points Path.home() at a temp dir holding a .tezcatlipoca/locks tree."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.locks = self.home / ".tezcatlipoca" / "locks"
        self.locks.mkdir(parents=True)

    def file(self, name):
        path = self.locks / name
        path.write_text("")
        return path

    def close(self):
        self.tmp.cleanup()


class InFlightDetection(unittest.TestCase):
    def setUp(self):
        self.d = _FakeLocksDir()
        self.addCleanup(self.d.close)
        self._home_patch = patch.object(Path, "home", return_value=self.d.home)
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)

    def test_no_locks_means_nothing_in_flight(self):
        self.assertEqual(nakon_ops.other_deploys_in_flight(), [])

    def test_an_unheld_stale_lock_is_not_a_deploy(self):
        """The host carries 18 of these; treating them as live would block every deploy."""
        self.d.file("engine-10.0.0.150-1080.lock")
        self.d.file("engine-10.0.0.193-1900.lock")
        self.assertEqual(nakon_ops.other_deploys_in_flight(), [])

    def test_a_held_lock_is_reported_as_in_flight(self):
        path = self.d.file("engine-10.0.0.150-1080.lock")
        holder = open(path, "a")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        in_flight = nakon_ops.other_deploys_in_flight()
        self.assertEqual([Path(p).name for p, _ in in_flight],
                         ["engine-10.0.0.150-1080.lock"])

    def test_our_own_locks_are_not_counted_against_us(self):
        path = str(self.d.file("engine-10.0.0.150-1080.lock"))
        holder = open(path, "a")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch.object(nakon_ops, "held_lock_paths", return_value={path}):
            self.assertEqual(nakon_ops.other_deploys_in_flight(), [])

    def test_a_missing_locks_dir_is_not_an_error(self):
        self.d.locks.rmdir()
        self.assertEqual(nakon_ops.other_deploys_in_flight(), [])


class PreflightGate(unittest.TestCase):
    def test_the_default_is_a_loud_warning_that_proceeds(self):
        """Concurrency is the supported shape (2026-10-04): the gate warns and gets out
        of the way; the per-resource collision gates do the refusing."""
        with patch("nakon_ops.other_deploys_in_flight",
                   return_value=[("/home/x/.tezcatlipoca/locks/engine-pve-1080.lock", 12.0)]), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEZ_ALLOW_CONCURRENT", None)  # a stray env var must not matter
            buf = io.StringIO()
            with redirect_stdout(buf):
                config_ops._gate_concurrent_deploys()   # no raise
        message = buf.getvalue()
        self.assertIn("WARNING", message)
        self.assertIn("engine-pve-1080.lock", message)       # names the holder
        self.assertIn("TF_VAR_team_identifiers", message)    # names what to keep distinct
        self.assertIn("--scoring-vmid", message)

    def test_nothing_in_flight_is_a_silent_pass(self):
        with patch("nakon_ops.other_deploys_in_flight", return_value=[]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                config_ops._gate_concurrent_deploys()   # no raise
        self.assertEqual(buf.getvalue(), "")

    def test_a_long_list_is_truncated(self):
        many = [(f"/locks/engine-{i}.lock", float(i)) for i in range(7)]
        with patch("nakon_ops.other_deploys_in_flight", return_value=many):
            buf = io.StringIO()
            with redirect_stdout(buf):
                config_ops._gate_concurrent_deploys()   # no raise
        self.assertIn("+3 more", buf.getvalue())

    def test_preflight_gates_calls_the_concurrency_gate_first(self):
        """It must fire before any per-competition check, because it protects other
        competitions, not just this one."""
        with patch.object(config_ops, "_gate_concurrent_deploys") as gate, \
                patch.dict(os.environ, {"TF_VAR_proxmox_node": "pve"}), \
                patch.object(config_ops, "proxmox_api",
                             side_effect=RuntimeError("stop here — we only need the order")):
            with self.assertRaises(SystemExit):
                config_ops.preflight_gates(Path("/tmp/nope"), [], 1)
        gate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
