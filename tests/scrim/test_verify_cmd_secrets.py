"""The verify command must survive a password that looks like an option.

The Quotient admin password is token_urlsafe(): it can start with "-". Passed as a separate
argv element, argparse reads "--admin-password -u!c..." as an option plus a missing value and
the authoritative pre-T0 gate dies with exit 2 — a deterministic failure that only shows up
when the dice land that way (live: cde-2026, run-7e7b7d81, 2026-11-24).
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from scrim import compworld  # noqa: E402

CREDS = {"ENGINE_IP": "10.0.0.253", "ADMIN_PW": "-u!c*i8btz-Bcn", "INJECT_PW": "",
         "BOX_PW": "n0t_sus1", "BOX_USER": "sysadmin", "KEY_PATH": "/k", "VM_USER": "ubuntu"}


class VerifyCmdTests(unittest.TestCase):
    def _cmd(self, password):
        creds = dict(CREDS, ADMIN_PW=password)
        with patch.object(compworld, "_verify_flags", return_value=[]):
            return compworld.verify_cmd("/tmp/comp", creds, "--red-identity")

    def test_a_hyphen_leading_password_rides_in_the_equals_form(self):
        cmd = self._cmd("-u!c*i8btz-Bcn")
        self.assertIn("--admin-password=-u!c*i8btz-Bcn", cmd)
        # the bare flag must never appear with the value as its own element
        self.assertNotIn("--admin-password", cmd)

    def test_an_ordinary_password_uses_the_same_form(self):
        self.assertIn("--admin-password=Abc123", self._cmd("Abc123"))
