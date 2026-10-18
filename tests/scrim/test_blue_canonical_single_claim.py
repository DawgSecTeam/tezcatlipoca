"""Only one blue workdir claims the canonical BLUE-TEAM.md.

With a workdir per team, two targets claiming one canonical name give the derivation two
candidates for a single document, and a later collection can flip which team's narrative becomes
the canonical one — caught live: sealed with "canonical document changed after
collection: BLUE-TEAM.md". Every team's REPORT.md must still be collected, under its own
evidence/blue/<team>/.
"""

import tempfile
import unittest
from pathlib import Path

from artifacts_lib import plan
from artifacts_lib.constants import SIDES


def _blue_targets(tmp, n_teams=2):
    workdirs = []
    for n in range(1, n_teams + 1):
        d = Path(tmp) / f"blue-team{n}"
        d.mkdir(exist_ok=True)
        workdirs.append(str(d))
    manifest = {"agents": {"blue": {"present": True, "teams": n_teams}},
                "paths": {"blue_workdirs": workdirs}}
    targets = plan.plan_targets(manifest, comp_dir=tmp)
    return [t for t in targets if str(t.get("name", "")).startswith("blue:")]


class SingleCanonicalClaim(unittest.TestCase):

    def test_exactly_one_team_claims_the_canonical_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            targets = _blue_targets(tmp)
            self.assertEqual(len(targets), 2)
            claims = [w for t in targets for w in t["want"] if w.get("canonical")]
        self.assertEqual([w["canonical"] for w in claims], [SIDES["blue"]])

    def test_every_team_still_contributes_its_own_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            targets = _blue_targets(tmp)
        for t in targets:
            self.assertIn("REPORT.md", [w["local_name"] for w in t["want"]])



if __name__ == "__main__":
    unittest.main()
