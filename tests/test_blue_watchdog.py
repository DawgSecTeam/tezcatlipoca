"""Blue watchdog script shape (winad-scrim2 rec 7). Pure string build; no SSH."""

import importlib.util
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("run_agent_scrim", _REPO / "run-agent-scrim.py")
scrim = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(scrim)


class WatchdogScript(unittest.TestCase):
    def test_units_and_masked_detection(self):
        s = scrim.watchdog_script(["nginx", "Enable WinRM", "bind"], "pw")
        self.assertIn("unmask nginx", s)
        self.assertIn("enable --now named", s)
        self.assertNotIn("WinRM", s)
        # `systemctl cat` fails on masked units — the exact case the watchdog must catch
        self.assertNotIn("systemctl cat", s)
        self.assertIn("list-unit-files nginx.service", s)

    def test_password_quoted(self):
        self.assertIn("""'a'"'"'b'""", scrim.watchdog_script(["nginx"], "a'b"))


if __name__ == "__main__":
    unittest.main()
