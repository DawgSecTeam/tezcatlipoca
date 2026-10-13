"""utils.retry: the shared attempts-with-gap loop pve_api.proxmox_api_for runs on."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import utils  # noqa: E402


class Retry(unittest.TestCase):
    def test_returns_first_success_without_sleeping(self):
        with patch.object(utils.time, "sleep") as slept:
            self.assertEqual(utils.retry(lambda: 7, attempts=4, gap=2), 7)
        slept.assert_not_called()

    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ValueError("later")
            return "ok"

        with patch.object(utils.time, "sleep"):
            self.assertEqual(utils.retry(flaky, attempts=4, gap=1, retry_on=ValueError), "ok")
        self.assertEqual(calls["n"], 3)

    def test_last_attempts_exception_propagates(self):
        with patch.object(utils.time, "sleep") as slept:
            with self.assertRaises(ValueError):
                utils.retry(lambda: (_ for _ in ()).throw(ValueError("x")),
                            attempts=3, gap=1, retry_on=ValueError)
        self.assertEqual(slept.call_count, 2)  # slept before attempts 2 and 3, not after the last

    def test_non_retry_on_exception_is_not_caught(self):
        with patch.object(utils.time, "sleep") as slept:
            with self.assertRaises(KeyError):
                utils.retry(lambda: (_ for _ in ()).throw(KeyError("k")),
                            attempts=3, gap=1, retry_on=ValueError)
        slept.assert_not_called()

    def test_backoff_multiplies_gap_by_attempt(self):
        waits = []
        with patch.object(utils.time, "sleep", side_effect=waits.append):
            with self.assertRaises(ValueError):
                utils.retry(lambda: (_ for _ in ()).throw(ValueError()),
                            attempts=4, gap=2, backoff=True, retry_on=ValueError)
        self.assertEqual(waits, [2, 4, 6])

    def test_constant_gap_without_backoff(self):
        waits = []
        with patch.object(utils.time, "sleep", side_effect=waits.append):
            with self.assertRaises(ValueError):
                utils.retry(lambda: (_ for _ in ()).throw(ValueError()),
                            attempts=3, gap=5, retry_on=ValueError)
        self.assertEqual(waits, [5, 5])


if __name__ == "__main__":
    unittest.main()
