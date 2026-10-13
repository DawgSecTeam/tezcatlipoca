"""The Windows wipe transport must survive real script sizes.

Three transports were live-proven and two broke (2026-10-12, iteration 2's
pre-T0 wipe): -EncodedCommand with the 103-path skip header crossed the Windows
shell's command-line limit ("The command line is too long.", instant fail), and
`powershell -Command -` parses stdin LINE BY LINE so the multi-line $skip array
broke with "Missing expression after ','". The transport is scp + -File; these
tests pin the shape.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scrim import clean_trails  # noqa: E402

CREDS = {"KEY_PATH": "/k", "VM_USER": "jump", "ENGINE_IP": "10.0.0.252",
         "BOX_USER": "medic", "BOX_PW": "pw"}


class WindowsWipeTransport(unittest.TestCase):
    def test_script_travels_as_a_file_not_the_command_line(self):
    # A 100+ path skip header must not ride argv (cmd.exe 8191-char cliff)
    #         nor stdin line-parsing (-Command - REPL semantics).
        seen = {}

        def fake_run(argv, **kwargs):
            joined = " ".join(str(a) for a in argv)
            seen.setdefault("legs", []).append(joined)
            if "scp" in argv[0]:
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0,
                             stdout="TRAILS_WIN_OK", stderr="")

        with mock.patch.object(clean_trails.procs, "run_tree", fake_run):
            r = clean_trails._run_windows_script(CREDS, "192.168.120.2",
                                                 "Write-Output 'x'" * 200)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(seen["legs"]), 2)            # scp up, then -File
        self.assertIn("scp", seen["legs"][0])
        self.assertIn("-File", seen["legs"][1])
        self.assertNotIn("EncodedCommand", seen["legs"][1])
        self.assertNotIn("Command -", seen["legs"][1])

    def test_scp_failure_is_reported_not_raised(self):
        with mock.patch.object(clean_trails.procs, "run_tree",
                               return_value=mock.Mock(returncode=255,
                                                      stdout="", stderr="nope")):
            r = clean_trails._run_windows_script(CREDS, "192.168.120.2", "x")
        self.assertEqual(r.returncode, 255)

    def test_cleanup_does_not_mask_the_powershell_exit_code(self):
    # The -File leg's rc must be PowerShell's: the staged file is deleted
    #         BEFORE it runs (`del ... &`), never chained after (`; del`).
        joined = []

        def fake_run(argv, **kwargs):
            joined.append(" ".join(str(a) for a in argv))
            return mock.Mock(returncode=0, stdout="TRAILS_WIN_OK", stderr="")

        with mock.patch.object(clean_trails.procs, "run_tree", fake_run):
            clean_trails._run_windows_script(CREDS, "192.168.120.2", "x")
        run_leg = joined[1]
        self.assertLess(run_leg.index("del"), run_leg.index("-File"))
        self.assertIn("&", run_leg)


if __name__ == "__main__":
    unittest.main()
