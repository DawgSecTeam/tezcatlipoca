"""The M4 reuse loop run twice (winad-testrun rec 2): three blockers — stranded clones, stale
domain markers, box-password churn — each broke only the SECOND run of a competition. This
drives phase 1's pure teardown decision across run 1 (3 teams) → teams-only teardown → run 2
(2 teams), offline."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import deploy
from golden_ops import golden_vmid_for
from range_ops import vm_id_for
from template_ops import engine_template_vmid

ENGINE = 1000
BOXES = [{"name": "dc01"}, {"name": "web01"}]
COMP_TAGS = {"tezcatlipoca", "comp-c1"}
TAGS = "tezcatlipoca;comp-c1"
HASHES = {"dc01": "h-dc", "web01": "h-web"}


def targets(idents):
    return [{"vmid": vm_id_for(i, b), "vm_name": f"{i}-{BOXES[b]['name']}"}
            for i in idents for b in range(len(BOXES))]


def node_after_run(idents):
    """What the node holds after a run: team boxes, engine, engine template, goldens."""
    vms = [{"vmid": t["vmid"], "name": t["vm_name"], "tags": TAGS} for t in targets(idents)]
    vms += [{"vmid": ENGINE, "tags": TAGS},
            {"vmid": engine_template_vmid(ENGINE), "tags": TAGS}]
    vms += [{"vmid": golden_vmid_for(ENGINE, i), "tags": TAGS + ";template"}
            for i in range(len(BOXES))]
    vms.append({"vmid": 4242, "name": "someone-else", "tags": "tezcatlipoca;comp-other"})
    return vms


class RepeatRun(unittest.TestCase):
    def waves(self, node_vms, idents, stored, frozen_keep=()):
        goldens = {golden_vmid_for(ENGINE, i) for i in range(len(BOXES))}
        with contextlib.redirect_stdout(io.StringIO()):
            return deploy.phase1_destroy_waves(
                node_vms, targets(idents), {}, ENGINE, BOXES, COMP_TAGS,
                lambda vid: vid in goldens, stored, HASHES, frozen_keep=frozen_keep)

    def test_second_run_with_fewer_teams(self):
        stored = {"golden": {n: {"hash": h} for n, h in HASHES.items()}}
        wave1, wave2 = self.waves(node_after_run([101, 102, 103]), [101, 102], stored)
        # team3's boxes are stranded clones of the goldens: they must die in wave 1
        for t in targets([103]):
            self.assertIn(t["vmid"], wave1)
        self.assertNotIn(4242, wave1)                       # other competition untouched
        self.assertNotIn(engine_template_vmid(ENGINE), wave1 | wave2)
        # hash-matching goldens are reused, not rebuilt; engine always re-clones
        for i in range(len(BOXES)):
            self.assertNotIn(golden_vmid_for(ENGINE, i), wave2)
        self.assertIn(ENGINE, wave2)

    def test_changed_golden_hash_rebuilds_only_that_golden(self):
        stored = {"golden": {"dc01": {"hash": "h-dc"}, "web01": {"hash": "OLD"}}}
        _w1, wave2 = self.waves(node_after_run([101, 102]), [101, 102], stored)
        self.assertNotIn(golden_vmid_for(ENGINE, 0), wave2)
        self.assertIn(golden_vmid_for(ENGINE, 1), wave2)

    def test_frozen_code_only_drift_keeps_the_golden(self):
        # Frozen comp, code-only drift: the pre-phase-1 gate said "proceeding on the
        # frozen template", so phase 1 must not destroy+rebuild the golden out from
        # under that decision (internals "frozen-gate golden keep").
        stored = {"golden": {"dc01": {"hash": "h-dc"}, "web01": {"hash": "OLD"}}}
        _w1, wave2 = self.waves(node_after_run([101, 102]), [101, 102], stored,
                                frozen_keep={"web01"})
        self.assertNotIn(golden_vmid_for(ENGINE, 1), wave2)
        self.assertNotIn(golden_vmid_for(ENGINE, 0), wave2)  # hash still matches
        self.assertIn(ENGINE, wave2)                          # engine still re-clones

    def test_frozen_keep_ignores_a_missing_template(self):
        # frozen_keep only protects an existing template; a deleted golden slot is
        # still rebuilt (there is nothing frozen to proceed on).
        stored = {"golden": {"dc01": {"hash": "h-dc"}, "web01": {"hash": "OLD"}}}
        goldens = {golden_vmid_for(ENGINE, 0)}  # web01's golden template is gone
        with contextlib.redirect_stdout(io.StringIO()):
            _w1, wave2 = deploy.phase1_destroy_waves(
                node_after_run([101, 102]), targets([101, 102]), {}, ENGINE, BOXES,
                COMP_TAGS, lambda vid: vid in goldens, stored, HASHES,
                frozen_keep={"web01"})
        self.assertIn(golden_vmid_for(ENGINE, 1), wave2)

    def test_unmanaged_box_slot_needs_no_hash_and_cleans_stale_golden(self):
        # Unmanaged boxes (pfSense) have no golden and no hash entry; the wave-2
        # decision must not KeyError on them and must treat a templated leftover in
        # their slot as stale (nothing references it).
        boxes = BOXES + [{"name": "fw01", "unmanaged": True}]
        goldens = {golden_vmid_for(ENGINE, i) for i in range(len(boxes))}
        fw_slot = golden_vmid_for(ENGINE, 2)
        vms = node_after_run([101]) + [{"vmid": fw_slot, "tags": TAGS + ";template"}]
        with contextlib.redirect_stdout(io.StringIO()):
            _w1, wave2 = deploy.phase1_destroy_waves(
                vms, targets([101]), {}, ENGINE, boxes, COMP_TAGS,
                lambda vid: vid in goldens,
                {"golden": {n: {"hash": h} for n, h in HASHES.items()}}, HASHES)
        self.assertIn(fw_slot, wave2)
        self.assertNotIn(golden_vmid_for(ENGINE, 0), wave2)
        self.assertNotIn(golden_vmid_for(ENGINE, 1), wave2)

    def test_domain_markers_reset_but_template_hashes_kept(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / ".nakon-domain-team1-adds.json").write_text("{}")
            (d / ".template-hashes.json").write_text("{}")
            deploy.reset_domain_markers(d)
            self.assertFalse((d / ".nakon-domain-team1-adds.json").exists())
            self.assertTrue((d / ".template-hashes.json").exists())

    def test_box_password_stable_across_runs(self):
        first = deploy.carry_box_password({})
        self.assertEqual(deploy.carry_box_password({"box_password": first}), first)
        self.assertTrue(deploy.carry_box_password(None))


if __name__ == "__main__":
    unittest.main()
