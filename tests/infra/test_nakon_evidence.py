"""A failed nakon plant must keep its own evidence."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from nakonops import nakon_run_ops as nro  # noqa: E402


class PreserveFailedRun(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def _runner(self, stdout=""):
        def _run(argv, timeout=90):
            self.calls.append(" ".join(argv[-1:]))
            return mock.Mock(returncode=0, stdout=stdout, stderr="")
        return _run


    def test_failed_run_is_moved_aside_and_tails_printed(self):
        runner = self._runner(stdout="--- log\nInstall-WindowsFeature : the error")
        failed = ["    [win02-golden]   └─ IIS FTP: FAILED rc=1 (5m51s)"]
        keep = nro._preserve_failed_run(["ssh", "engine"],
                                        "/opt/nakon/golden", failed,
                                        runner=runner)
        self.assertIsNotNone(keep)
        self.assertTrue(keep.startswith("/opt/nakon/failed-golden-"))
        self.assertEqual(len(self.calls), 2)
        self.assertIn("sudo mv /opt/nakon/golden", self.calls[0])
        self.assertIn("win02-golden.log", self.calls[1])
        self.assertIn("win02-golden.log", keep and self.calls[1])

    def test_missing_staging_reports_instead_of_raising(self):
        def _run(argv, timeout=90):
            return mock.Mock(returncode=1, stdout="", stderr="no such file")
        keep = nro._preserve_failed_run(["ssh"], "/opt/nakon/golden",
                                        ["[web01-golden] └─ nginx: FAILED rc=1"],
                                        runner=_run)
        self.assertIsNone(keep)



if __name__ == "__main__":
    unittest.main()


class SessionDeathRetry(unittest.TestCase):
    # A dead ssh session may re-run the bundle only when the death looks cold (fast, before
    # any step line streamed): a blind retry after partial execution re-promotes live DCs.
    def _run(self, popen_results):
        made = []
        def fake_popen(cmd, **kw):
            made.append(cmd)
            return popen_results[min(len(made), len(popen_results)) - 1]
        with mock.patch.object(nro.subprocess, "run",
                               side_effect=lambda *a, **kw: mock.Mock(returncode=0)), \
                mock.patch.object(nro.subprocess, "Popen", side_effect=fake_popen), \
                mock.patch.object(nro.time, "sleep"):
            try:
                nro.run_nakon("key", "sysadmin", "10.0.0.249",
                              Path("/tmp/b.tar.gz"), Path("/tmp/cfg.json"))
            except RuntimeError as e:
                self.last_error = e
            else:
                self.last_error = None
        return made

    def _proc(self, lines, rc):
        proc = mock.Mock()
        proc.stdout = iter(lines)
        proc.wait = mock.Mock()
        proc.returncode = rc
        return proc

    def test_midrun_death_is_not_retried(self):
        made = self._run([self._proc(
            ["[dc01-team130] step 000/1 config: ADDS\n"], 255)])
        self.assertEqual(len(made), 1, "a death after steps ran must not re-run the bundle")
        self.assertIn("resume the phase", str(self.last_error))

    def test_cold_death_retries_then_reports(self):
        made = self._run([self._proc([], 255)] * 3)
        self.assertEqual(len(made), 3, "a cold death keeps the 3-attempt retry")
        self.assertIn("rc=255", str(self.last_error))
