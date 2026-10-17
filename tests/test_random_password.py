"""config_ops.random_password charset: the incidents hung on it (a live scrim).

A letters+digits pool produced digit-free passwords that AD complexity policy rejected, and
a forbidden character in the pool breaks DSN/quoting. Pin 14 chars, every class present, and
nothing outside the cmd/PS- and URL-safe alphabet.
"""

import string
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import config_ops  # noqa: E402


class RandomPasswordTests(unittest.TestCase):

    def test_charset_and_guarantees_are_unchanged(self):
        allowed = set(string.ascii_uppercase + string.ascii_lowercase + string.digits
                      + "!*_-+=")
        pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!*_-+="]
        seen = set()
        for _ in range(50):
            password = config_ops.random_password()
            self.assertEqual(len(password), 14)
            self.assertTrue(set(password) <= allowed, password)
            for pool in pools:
                self.assertTrue(any(ch in pool for ch in password), password)
            seen |= set(password)
        for forbidden in "#/?@ &|<>^%$`\"'":
            self.assertNotIn(forbidden, seen,
                             f"{forbidden!r} must never be drawn (DSN/quoting hazard)")


if __name__ == "__main__":
    unittest.main()
