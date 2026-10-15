"""estate.destroy: teardown tier order, and a refusal that never aborts the tier.

Linked clones share their golden's base volume (logs/destroy-diag/run-fa882ee8.log), so the
delete sequence is an invariant: clones, goldens, engine template, engine, bridges. A refused
resource stays in state and is reported; everything else still dies. Offline: fake Proxmox API,
a temp comp dir per test."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from estate import destroy  # noqa: E402
from estate.state import EstateState  # noqa: E402
from infra import client, ownership  # noqa: E402

COMP = "c1"
RUN = "run-abcdef01"
BASE = {"tezcatlipoca", f"comp-{COMP}", RUN}
GOLD = BASE | {"tezcatlipoca-golden"}
ENGINE_TEMPLATE = BASE | {"engine-template"}


class FakePVE:
    """Minimal PVE: node VM listings, per-VM configs, DELETE capture."""

    def __init__(self, nodes):
        self.nodes = nodes
        self.deleted = []      # (node, vmid) in call order
        self.bridges = []      # (node, bridge_name) in call order

    def __call__(self, method, path, **kw):
        if method == "GET" and path.endswith("/qemu"):
            return {"data": [dict(v) for v in self.nodes.get(path.split("/nodes/")[1].split("/")[0], [])]}
        if method == "GET" and path.endswith("/config"):
            vmid = int(path.split("/")[-2])
            return {"data": next((v for vs in self.nodes.values() for v in vs if v["vmid"] == vmid), {})}
        if method == "GET" and path.endswith("/network"):
            return {"data": [{"iface": "vmbr101"}]}
        if method == "GET" and path.endswith("/storage"):
            return {"data": []}
        if method == "DELETE" and "/network/" in path:
            self.bridges.append((path.split("/nodes/")[1].split("/")[0], path.rsplit("/", 1)[1]))
            return {"data": None}
        if method == "DELETE" and "/qemu/" in path:
            self.deleted.append((path.split("/nodes/")[1].split("/")[0], int(path.rsplit("/", 1)[1])))
            return {"data": None}
        return {"data": None}


def vm(vmid, name, tags, status="stopped"):
    return {"vmid": vmid, "name": name, "tags": ";".join(sorted(tags)), "status": status}


def full_state(comp_dir, tags_by_vmid=None):
    """One resource of every kind, with the tags the deploy stamps (role tag included)."""
    tags_by_vmid = tags_by_vmid or {}
    state = EstateState.load(comp_dir)
    state.data["identity"] = {"comp_name": COMP, "run_id": RUN}
    for kind, name, vmid, tags in (
            ("team_box", "team1-web01", 2001, BASE),
            ("jump", "jump-a", 2002, BASE),
            ("golden", "web01-golden", 1151, GOLD),
            ("engine_template", "engine-template", 1140, ENGINE_TEMPLATE),
            ("engine", "scoring-engine", 1000, BASE)):
        state.record(kind, "pve", name, vmid=vmid, tags=sorted(tags_by_vmid.get(vmid, tags)))
    state.record("bridge", "pve", "vmbr101", bridge="vmbr101", tags=[])
    return state


def fake_for(state, tags_by_vmid=None):
    tags_by_vmid = tags_by_vmid or {}
    vms = []
    for row in state.resources():
        if row["vmid"] is not None:
            vms.append(vm(row["vmid"], row["name"], tags_by_vmid.get(row["vmid"], set(row["tags"]))))
    return FakePVE({"pve": vms})


class FakeAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.comp = Path(self.tmp.name) / "comp-a"
        self.comp.mkdir()

    def patch_api(self, state, tags_by_vmid=None, fake=None):
        self.fake = fake or fake_for(state, tags_by_vmid)
        patch.object(ownership, "wait_for_proxmox_task", lambda *a, **k: None).start()
        for module in (ownership, client):
            patch.object(module, "proxmox_api", self.fake).start()
        self.addCleanup(patch.stopall)
        return self.fake

    def kinds_of(self, entries):
        return [e["kind"] for e in entries]


class TierOrder(FakeAPI):
    def test_clones_die_before_goldens_before_engine_template_before_engine_before_bridges(self):
        state = full_state(self.comp)
        fake = self.patch_api(state)
        summary = destroy.destroy_estate(state, log=lambda *a: None)
        self.assertEqual(sorted(self.kinds_of(summary["destroyed"])),
                         ["bridge", "engine", "engine_template", "golden", "jump", "team_box"])
        self.assertEqual([vmid for _, vmid in fake.deleted], [2001, 2002, 1151, 1140, 1000])
        self.assertEqual(fake.bridges, [("pve", "vmbr101")])
        # The cross-tier rule TIERS encodes, asserted on the flat sequence.
        self.assertLess(fake.deleted.index(("pve", 1151)), fake.deleted.index(("pve", 1140)))
        self.assertLess(fake.deleted.index(("pve", 1140)), fake.deleted.index(("pve", 1000)))

    def test_no_golden_delete_precedes_the_last_clone_delete(self):
        state = full_state(self.comp)
        fake = self.patch_api(state)
        destroy.destroy_estate(state, log=lambda *a: None)
        clones = [i for i, (_, vmid) in enumerate(fake.deleted) if vmid in (2001, 2002)]
        goldens = [i for i, (_, vmid) in enumerate(fake.deleted) if vmid == 1151]
        self.assertTrue(clones and goldens)
        self.assertGreater(min(goldens), max(clones))

    def test_destroy_tier_returns_the_three_lists(self):
        state = full_state(self.comp)
        self.patch_api(state)
        destroyed, refused, skipped = destroy.destroy_tier(state, "golden", log=lambda *a: None)
        self.assertEqual([e["vmid"] for e in destroyed], [1151])
        self.assertEqual((refused, skipped), ([], []))
        self.assertEqual(state.resources(kind="golden"), [])


class RefusalNeverAborts(FakeAPI):
    def test_refused_team_box_stays_in_state_and_the_rest_still_dies(self):
        state = full_state(self.comp, tags_by_vmid={2001: {"tezcatlipoca", f"comp-{COMP}"}})
        fake = self.patch_api(state, tags_by_vmid={2001: {"tezcatlipoca", f"comp-{COMP}"}})
        summary = destroy.destroy_estate(state, log=lambda *a: None)
        self.assertEqual([vmid for _, vmid in fake.deleted], [2002, 1151, 1140, 1000],
                         "the jump must die despite a refused sibling")
        self.assertEqual([r["entry"]["vmid"] for r in summary["refused"]], [2001])
        self.assertIn("refusing to destroy", summary["refused"][0]["reason"])
        self.assertIn(RUN, summary["refused"][0]["reason"])
        self.assertEqual([e["vmid"] for e in state.resources(kind="team_box")], [2001])
        self.assertEqual(len(summary["destroyed"]), 5)


class Idempotent(FakeAPI):
    def test_already_absent_vms_are_forgotten_and_nothing_is_refused(self):
        state = full_state(self.comp)
        self.patch_api(state, fake=FakePVE({"pve": []}))
        summary = destroy.destroy_estate(state, log=lambda *a: None)
        self.assertEqual(summary["refused"], [])
        self.assertEqual(len(summary["destroyed"]), 6)
        self.assertEqual(state.resources(), [])


class ForgetAndSave(FakeAPI):
    def test_destroyed_entries_leave_the_state_and_it_is_saved(self):
        state = full_state(self.comp)
        self.patch_api(state)
        state.save = Mock(wraps=state.save)
        destroy.destroy_estate(state, log=lambda *a: None)
        self.assertTrue(state.save.called)
        self.assertEqual(state.resources(), [])
        self.assertEqual(EstateState.load(self.comp).resources(), [])


class TeamsOnly(FakeAPI):
    def test_templates_survive_and_are_reported_skipped(self):
        state = full_state(self.comp)
        self.patch_api(state)
        summary = destroy.destroy_estate(state, full=False, log=lambda *a: None)
        self.assertEqual(sorted(self.kinds_of(summary["skipped"])), ["engine_template", "golden"])
        self.assertEqual(sorted((e["kind"], e["vmid"]) for e in summary["destroyed"]),
                         [("bridge", None), ("engine", 1000), ("jump", 2002), ("team_box", 2001)])
        self.assertEqual([e["kind"] for e in state.resources()], ["golden", "engine_template"])
        self.assertEqual(summary["refused"], [])


class NodeFilter(FakeAPI):
    def test_an_entry_outside_the_placement_hosts_is_skipped_untouched(self):
        state = EstateState.load(self.comp)
        state.data["identity"] = {"comp_name": COMP, "run_id": RUN}
        state.record("team_box", "pve", "team1-web01", vmid=2001, tags=sorted(BASE))
        state.record("team_box", "other", "team2-web01", vmid=2002, tags=sorted(BASE))
        fake = self.patch_api(state)
        summary = destroy.destroy_estate(state, nodes={"pve"}, log=lambda *a: None)
        self.assertEqual([vmid for _, vmid in fake.deleted], [2001])
        self.assertEqual([e["vmid"] for e in summary["skipped"]], [2002])
        self.assertEqual([e["vmid"] for e in state.resources()], [2002])


if __name__ == "__main__":
    unittest.main()
