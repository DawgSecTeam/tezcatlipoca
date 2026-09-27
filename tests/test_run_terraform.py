"""run_terraform: process-group isolation so an interrupted driver can't orphan terraform.
Uses a fake `terraform` shell script on PATH; no real terraform or Proxmox."""

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import utils


def _fake_terraform(dirpath, body):
    p = Path(dirpath) / "terraform"
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(0o755)


class RunTerraform(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._path = os.environ["PATH"]
        os.environ["PATH"] = self.tmp.name + os.pathsep + self._path

    def tearDown(self):
        os.environ["PATH"] = self._path
        self.tmp.cleanup()

    def test_success_and_check(self):
        _fake_terraform(self.tmp.name, "exit 0\n")
        self.assertEqual(utils.run_terraform(["apply"], cwd=self.tmp.name).returncode, 0)
        _fake_terraform(self.tmp.name, "exit 3\n")
        with self.assertRaises(subprocess.CalledProcessError):
            utils.run_terraform(["apply"], cwd=self.tmp.name)
        self.assertEqual(utils.run_terraform(["apply"], cwd=self.tmp.name, check=False).returncode, 3)

    def test_timeout_reaps_group(self):
        pidfile = Path(self.tmp.name) / "pid"
        # a grandchild (sleep) that would outlive a naive kill of the direct child
        _fake_terraform(self.tmp.name, f"sleep 300 &\necho $! > {pidfile}\nwait\n")
        with self.assertRaises(subprocess.TimeoutExpired):
            utils.run_terraform(["apply"], cwd=self.tmp.name, timeout=1, grace=5)
        gpid = int(pidfile.read_text())
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(gpid, 0)

    def test_sigterm_to_driver_reaps_group(self):
        pidfile = Path(self.tmp.name) / "pid"
        _fake_terraform(self.tmp.name, f"sleep 300 &\necho $! > {pidfile}\nwait\n")
        threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        with self.assertRaises(KeyboardInterrupt):
            utils.run_terraform(["apply"], cwd=self.tmp.name, grace=5)
        gpid = int(pidfile.read_text())
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(gpid, 0)


if __name__ == "__main__":
    unittest.main()
