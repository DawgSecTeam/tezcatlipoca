"""Blue watchdog script shape (winad-scrim2 rec 7). Pure string build; no SSH."""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scrim import blue_watchdog


class WatchdogScript(unittest.TestCase):
    def test_units_and_masked_detection(self):
        s = blue_watchdog.watchdog_script(["nginx", "Enable WinRM", "bind"], "pw")
        self.assertIn("unmask nginx", s)
        self.assertIn("enable --now named", s)
        self.assertNotIn("WinRM", s)
        # `systemctl cat` fails on masked units — the exact case the watchdog must catch
        self.assertNotIn("systemctl cat", s)
        self.assertIn("list-unit-files nginx.service", s)

    def test_password_quoted(self):
        self.assertIn("""'a'"'"'b'""", blue_watchdog.watchdog_script(["nginx"], "a'b"))


if __name__ == "__main__":
    unittest.main()
