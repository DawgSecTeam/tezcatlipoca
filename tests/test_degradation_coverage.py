"""Every "we carried on anyway" site must feed the degradation ledger.

Why this file exists: the tolerated-failure ledger was added with ten sites wired by
hand — and the very next live run printed a root-disk expansion failure
(`expansion failed (size unmeasurable) — continuing`) and two failed pool snapshots
that never reached it, so `verify` reported `degradations: 0` for a run that had
visibly degraded. A manual sweep is how that happens; a scan is not.

The rule: a `print(... WARNING ... proceeding|continuing)` in non-test source must have
a `record_degradation(` call within three lines. A site that is deliberately *not* a
degradation goes in ALLOWED with a reason — the point is that nobody gets to skip the
decision by accident.
"""

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import range_ops  # noqa: E402
import vm_lifecycle  # noqa: E402
import utils  # noqa: E402

# Directories that are not this repo's source (a stray copy must not be scanned).
_SKIP = ("vendor/", "tests/", ".tez-backups/", ".worktrees/")
_FLAG = re.compile(r"print\(.*WARNING.*(proceeding|continuing)", re.IGNORECASE)

# Sites that intentionally continue WITHOUT a ledger entry, each with its reason.
# Keyed on "<file>:<a distinctive substring of the line>".
ALLOWED = {
    # (empty today — everything found by the scan records. Add with a reason, not
    #  silently: an unexplained entry defeats the point of the guard.)
}


def _scan():
    """[(path, line_number, line, has_ledger_call)] for every flagged site."""
    findings = []
    for path in sorted(Path(_REPO).rglob("*.py")):
        rel = str(path.relative_to(_REPO))
        if any(part in rel for part in _SKIP):
            continue
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            if not _FLAG.search(line):
                continue
            window = "\n".join(lines[max(0, i - 3):i + 4])
            findings.append((rel, i + 1, line.strip(), "record_degradation(" in window))
    return findings


class EveryWarningSiteFeedsTheLedger(unittest.TestCase):
    def test_the_scan_finds_the_sites_it_is_supposed_to(self):
        """A guard that matches nothing is not a guard — pin that it sees real sites."""
        findings = _scan()
        self.assertGreaterEqual(len(findings), 8, "the scan stopped matching anything")
        self.assertTrue(any("apt prep" in line for _, _, line, _ in findings))

    def test_no_flagged_site_is_missing_a_ledger_call(self):
        gaps = []
        for rel, lineno, line, has_ledger in _scan():
            if has_ledger:
                continue
            if any(rel == key.split(":")[0] and key.split(":", 1)[1] in line
                   for key in ALLOWED):
                continue
            gaps.append(f"{rel}:{lineno}: {line[:90]}")
        self.assertEqual(
            gaps, [],
            "these sites warn and carry on without recording a degradation — either wire "
            "them (record_degradation) or add them to ALLOWED with a reason:\n  "
            + "\n  ".join(gaps))


class SnapshotFailureIsRecorded(unittest.TestCase):
    """A failed snapshot is not cosmetic: it is the rollback point."""

    def setUp(self):
        utils.clear_degradations()
        self.addCleanup(utils.clear_degradations)

    def test_a_failed_snapshot_records_a_degradation(self):
        def failing_api(method, path, **kwargs):
            raise RuntimeError("zfs error: cannot create snapshot 'hdd/vm-1500-disk-0@tz-base': "
                               "out of space")
        with patch.object(vm_lifecycle, "list_snapshots", return_value=set()), \
                patch.object(vm_lifecycle, "proxmox_api", side_effect=failing_api), \
                patch("builtins.print"):
            ok = range_ops.take_snapshot("proxmox", 1500, "tz-base")
        self.assertFalse(ok)                       # still never raises, by design
        entries = utils.degradations()
        self.assertEqual(len(entries), 1)
        self.assertIn("tz-base", entries[0]["what"])
        self.assertIn("1500", entries[0]["detail"])
        self.assertIn("out of space", entries[0]["detail"])

    def test_a_successful_snapshot_records_nothing(self):
        with patch.object(vm_lifecycle, "list_snapshots", return_value=set()), \
                patch.object(vm_lifecycle, "proxmox_api",
                             return_value={"data": "UPID:pve:0001"}), \
                patch.object(vm_lifecycle, "wait_for_proxmox_task"), \
                patch("builtins.print"):
            ok = range_ops.take_snapshot("proxmox", 1500, "tz-ready")
        self.assertTrue(ok)
        self.assertEqual(utils.degradations(), [])


if __name__ == "__main__":
    unittest.main()
