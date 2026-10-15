"""The M4 reuse loop run twice, offline: the per-comp-dir state hazards that broke only
the SECOND run of a competition — stale domain markers and box-password churn.

Template reuse is phase 4's gate now (`golden_rebuild_gate` + `template_stamp_current`,
covered by test_deploy_phases / test_freeze_guard), and the preflight rule that keeps a
--teams-only teardown's kept templates out of the collision set is covered by
test_preflight_unified. Phase 1 no longer destroys anything a run owns.
"""

import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from deploy_lib.phases import cleanup as dl_cleanup  # noqa: E402
from deploy_lib import secrets as dl_secrets  # noqa: E402


class RepeatRun(unittest.TestCase):
    def test_domain_markers_reset_but_template_hashes_kept(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / ".nakon-domain-team1-adds.json").write_text("{}")
            (d / ".template-hashes.json").write_text("{}")
            dl_cleanup.reset_domain_markers(d)
            self.assertFalse((d / ".nakon-domain-team1-adds.json").exists())
            self.assertTrue((d / ".template-hashes.json").exists())

    def test_box_password_stable_across_runs(self):
        first = dl_secrets.carry_box_password({})
        self.assertEqual(dl_secrets.carry_box_password({"box_password": first}), first)
        self.assertTrue(dl_secrets.carry_box_password(None))


if __name__ == "__main__":
    unittest.main()
