"""The prepared probe must not turn its own transient failure into a refused restore.

A rollback reboots every VM. A Windows guest agent needs a while after that boot, and a single
read against it answers "unreachable:HTTPError" for a box that is coming up fine — compared with
the reference, that refused a range that had been restored (live: cde-2026 ftp01, run-11fe6541).
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from prepared import fingerprint  # noqa: E402


class AgentRetry(unittest.TestCase):
    def test_a_box_that_answers_on_a_retry_is_measured(self):
        runs = [RuntimeError("HTTPError"), RuntimeError("HTTPError"), (0, "80\n443\n", "")]
        with patch.object(fingerprint, "guest_agent_exec_root", side_effect=runs), \
                patch.object(fingerprint.time, "sleep"):
            raw = fingerprint._agent_exec("node", 1761, "ss", windows=False, attempts=5)
        self.assertEqual(raw, "80\n443\n")

    def test_a_box_that_never_answers_is_reported_unreachable(self):
        with patch.object(fingerprint, "guest_agent_exec_root",
                          side_effect=RuntimeError("HTTPError")), \
                patch.object(fingerprint.time, "sleep"):
            raw = fingerprint._agent_exec("node", 1761, "ss", windows=False, attempts=3)
        self.assertEqual(raw, "unreachable:RuntimeError")


class UnmeasuredFields(unittest.TestCase):
    def test_a_probe_failure_is_named_not_silently_compared(self):
        diffs = ["boxes.team2-ftp01: at-snapshot=[21, 22] after-rollback='unreachable:HTTPError'",
                 "boxes.team1-web01: at-snapshot=[80] after-rollback=[80]"]
        self.assertEqual(fingerprint._unmeasured(diffs), ["boxes.team2-ftp01"])

    def test_a_plain_delta_is_not_a_probe_failure(self):
        self.assertEqual(fingerprint._unmeasured(
            ["boxes.team1-web01: at-snapshot=[80] after-rollback=[8080]"]), [])
