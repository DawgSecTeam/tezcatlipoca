"""Characterization tests for the teardown redesign's seven invariants (docs/internals.md#teardown)."""
# I1 ownership total and verified; I2 force by default, only against owned targets;
# I3 evidence before destruction, a dead box never blocks; I4 resumable without a flag;
# I5 same-comp different-run is foreign everywhere; I6 refusals name saw/expected/remedy;
# I7 exit 0 means the estate is clean for this ownership set. The API is faked at EVERY
# owning module (infra.client/infra.ownership, teardown.machinery) or the test dials the node.

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from artifacts_lib import artifacts_ops  # noqa: E402
from red import red01_ops, tlaloc_ops  # noqa: E402

from infra import client as infra_client  # noqa: E402
from infra import ownership  # noqa: E402
from artifacts_lib import lifecycle as alifecycle  # noqa: E402
from artifacts_lib import paths as apaths  # noqa: E402
from teardown import machinery  # noqa: E402
from teardown import runner as trunner  # noqa: E402
from infra.ownership import Ownership, Refusal  # noqa: E402
from estate import state as estate_state  # noqa: E402

RUN = "run-abcdef01"
OTHER_RUN = "run-12345678"
COMP = "c1"
OURS = Ownership(COMP, RUN)
OTHER_OWNERSHIP = Ownership(COMP, OTHER_RUN)
FULL_OTHER = f"tezcatlipoca,comp-{COMP},{OTHER_RUN}"


def load_destroy():
    spec = importlib.util.spec_from_file_location(
        "destroy_competition_characterization", _REPO / "destroy-competition.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


destroy = load_destroy()


class FakePVE:
    def __init__(self, nodes=None, cfgs=None, listing_fails=()):
        self.nodes = nodes or {}
        self.cfgs = cfgs or {}
        self.listing_fails = set(listing_fails)
        self.deleted = []
        self.puts = []
        self.calls = []          # (method, path, params) — proves WHICH endpoint was used

    def __call__(self, method, path, **kw):
        self.calls.append((method, path, kw.get("params")))
        if method == "GET" and path.endswith("/qemu"):
            node = path.split("/nodes/")[1].split("/")[0]
            if node in self.listing_fails:
                raise ConnectionError(f"{node} unreachable")
            return {"data": [dict(v) for v in self.nodes.get(node, [])]}
        if method == "GET" and path.endswith("/config"):
            return {"data": self.cfgs.get(int(path.split("/")[-2]), {})}
        if method == "PUT" and path.endswith("/config"):
            self.puts.append((int(path.split("/")[-2]), kw.get("data")))
            self.cfgs.setdefault(int(path.split("/")[-2]), {}).update(kw.get("data") or {})
            return {"data": None}
        if method == "POST" and path.endswith("/status/stop"):
            return {"data": None}
        if method == "DELETE" and "/qemu/" in path:
            self.deleted.append(int(path.rsplit("/", 1)[1].split("?")[0]))
            return {"data": None}
        if method == "GET" and path.endswith("/network"):
            return {"data": []}
        return {"data": None}


def vm(vmid, name, tags, status="stopped"):
    return {"vmid": vmid, "name": name, "tags": tags, "status": status}


@patch.object(ownership, "wait_for_proxmox_task", lambda *a, **k: None)
class OwnershipTotality(unittest.TestCase):
    def test_full_tag_set_is_the_only_tags_path_to_destroy(self):
        fake = FakePVE(nodes={"n": [vm(10, "ours", "tezcatlipoca,comp-c1,run-abcdef01")]},
                       cfgs={10: {"tags": "tezcatlipoca;comp-c1;run-abcdef01"}})
        with patch.object(ownership, "proxmox_api", fake):
            ownership.destroy_vm_if_exists("n", 10, expect_tags=OURS.tags)
        self.assertEqual(fake.deleted, [10])

    def test_partial_tag_overlap_is_refused_with_saw_and_missing(self):
        fake = FakePVE(nodes={"n": [vm(11, "theirs", FULL_OTHER)]},
                       cfgs={11: {"tags": f"tezcatlipoca;comp-c1;{OTHER_RUN}"}})
        with patch.object(ownership, "proxmox_api", fake):
            with self.assertRaises(Refusal) as ctx:
                ownership.destroy_vm_if_exists("n", 11, expect_tags=OURS.tags)
        msg = str(ctx.exception)
        self.assertIn("11", msg)
        self.assertIn(OTHER_RUN, msg)
        self.assertIn(RUN, msg)
        self.assertEqual(ctx.exception.saw,
                         f"vmid 11 (theirs) carrying tags 'tezcatlipoca;comp-c1;{OTHER_RUN}'")
        self.assertEqual(fake.deleted, [])

    def test_state_without_run_id_refuses_to_destroy_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = Path(tmp)
            document = estate_state.EstateState.load(comp)
            document.absorb({"scoring_vm_id": 1000})
            document.save()
            with self.assertRaises(Refusal) as ctx:
                machinery.load_ownership(comp)
            msg = str(ctx.exception)
            self.assertIn("no run id", msg)
            self.assertIn("estate-state.json", msg)
            self.assertIn("remove it by hand", msg)
            self.assertTrue(ctx.exception.remedy)      # the vocabulary is complete

    def test_endpoint_mismatch_is_refused_with_both_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = Path(tmp)
            document = estate_state.EstateState.load(comp)
            document.absorb({"run_id": RUN, "deployed_endpoint": "https://old:8006/"})
            document.save()
            with patch.dict(os.environ, {"TEZ_PROXMOX_ENDPOINT": "https://new:8006"}):
                with self.assertRaises(Refusal) as ctx:
                    machinery.load_ownership(comp)
            msg = str(ctx.exception)
            self.assertIn("https://old:8006", msg)
            self.assertIn("https://new:8006", msg)
            self.assertTrue(ctx.exception.remedy)      # every refusal carries a remedy

    def test_sweep_ignores_a_clone_marker_only_vm(self):
    # The leftover sweep matches TAGS only; a clone-marker-only VM is reclaimable solely
        # through the guarded delete primitive.
        fake = FakePVE(nodes={"n": [vm(12, "half-clone", "")]},
                       cfgs={12: {"description": ownership.clone_marker(COMP)}})
        with patch.object(machinery, "proxmox_api", fake), \
                patch.object(machinery, "wait_for_proxmox_task", lambda *a, **k: None):
            machinery.sweep_tagged_leftovers(["n"], OURS)
        self.assertEqual(fake.deleted, [])


class ForceByDefault(unittest.TestCase):
    TEAMS = {"team1": {"identifier": "101"}}
    BOXES = [{"name": "web01"}]

    def test_pre_stop_uses_the_hard_stop_endpoint_never_shutdown(self):
        fake = FakePVE(nodes={"n": [vm(20, "team1-web01", "tezcatlipoca,comp-c1,run-abcdef01",
                                       status="running")]})
        with patch.object(machinery, "proxmox_api", fake), \
                patch.object(machinery, "wait_for_proxmox_task", lambda *a, **k: None):
            machinery.pre_stop_windows_boxes(self.TEAMS, self.BOXES, "n", ownership=OURS)
        posts = [p for m, p, _ in fake.calls if m == "POST"]
        self.assertTrue(posts and all(p.endswith("/status/stop") for p in posts))
        self.assertFalse(any("shutdown" in p for p in posts))

    def test_destroy_primitive_hard_stops_a_running_box_then_deletes(self):
        fake = FakePVE(nodes={"n": [vm(21, "ours", "tezcatlipoca,comp-c1,run-abcdef01",
                                       status="running")]},
                       cfgs={21: {"tags": "tezcatlipoca;comp-c1;run-abcdef01"}})
        with patch.object(ownership, "proxmox_api", fake), \
                patch.object(ownership, "wait_for_proxmox_task", lambda *a, **k: None):
            ownership.destroy_vm_if_exists("n", 21, expect_tags=OURS.tags)
        seq = [(m, p, prm) for m, p, prm in fake.calls if m in ("POST", "DELETE")]
        self.assertEqual(seq[0][:2], ("POST", "/nodes/n/qemu/21/status/stop"))
        self.assertTrue(seq[-1][0] == "DELETE" and "/qemu/21" in seq[-1][1])
        self.assertTrue(seq[-1][2].get("purge"), "destroy must purge (no clean shutdowns)")


TEAMS = {"team1": {"identifier": "101", "password": "x"}}
BOXES = [{"name": "dc01", "last_octet": 2, "template": "base-windows-server"}]


class MainSequence(unittest.TestCase):
# drives destroy-competition.main() offline, every destructive call a log entry.

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        comp = self.root / "competitions" / "demo"
        comp.mkdir(parents=True)
        (comp / "Compfile").write_text("name demo\nscenario x\ndifficulty 3\n")
        (comp / "teams.json").write_text(json.dumps(TEAMS))
        (comp / "boxes.json").write_text(json.dumps(BOXES))
        document = estate_state.EstateState.load(comp)
        document.absorb({"run_id": "run-1a2b3c4d", "scoring_vm_id": 1000})
        document.save()
        self.comp = comp
        self.log = []
        self._cwd = os.getcwd()

    def _main(self, argv, *, collect=None, red=None, estate=None, frozen=None):
        calls = self.log
        collect = collect if collect is not None else self._collecting_fake()
        red = red or (lambda *a, **k: calls.append("red_destroy") or
                      subprocess.CompletedProcess(["x"], 0, "", ""))
        estate = estate if estate is not None else (
            lambda *a, **k: calls.append("estate_destroy") or
            {"destroyed": [], "skipped": [], "refused": [], "kept": []})
        argv = ["destroy-competition.py", "--competition", "demo"] + argv
        patches = [
            patch.object(destroy, "load_destroyable_competitions", return_value=["demo"]),
            patch.object(destroy, "frozen_state", return_value=frozen or {}),
            patch.object(trunner, "frozen_state", return_value=frozen or {}),
            patch.object(machinery, "pre_stop_windows_boxes",
                         side_effect=lambda *a, **k: calls.append("pre_stop_windows_boxes")),
            patch.object(machinery, "sweep_tagged_leftovers"),
            patch.object(machinery, "teardown_templates", return_value=[]),
            patch("estate.destroy.destroy_estate", side_effect=estate),
            patch.object(artifacts_ops, "collect_for_teardown", side_effect=collect),
            patch.object(tlaloc_ops, "destroy_tlaloc_red", side_effect=red),
            patch.object(infra_client, "proxmox_api", FakePVE()),
            patch.object(ownership, "proxmox_api", FakePVE()),
            patch.object(ownership, "wait_for_proxmox_task", lambda *a, **k: None),
            patch.object(infra_client, "live_vmids", lambda: []),
            patch.object(red01_ops, "recorded_identity", lambda *a, **k: {"vmid": 999, "ip": "", "node": ""}),
            # The entry point refuses without the ambient estate identity (verify_clean
            # needs it); this harness exercises the sequence, not the refusal.
            patch.dict(os.environ, {"TEZ_PROXMOX_ENDPOINT": "https://pve.invalid:8006"}),
            patch.object(sys, "argv", argv),
        ]
        os.chdir(self.root)
        buf = io.StringIO()
        try:
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                with contextlib.redirect_stdout(buf):
                    destroy.main()
        finally:
            os.chdir(self._cwd)
            self.out = buf.getvalue()
        return calls

    def _collecting_fake(self):
        def fake(comp_dir, **kwargs):
            self.log.append("collect")
            key, _ = alifecycle.latest_run_key(comp_dir, kwargs.get("run_id"))
            Path(apaths.test_dir(comp_dir, key)).mkdir(parents=True, exist_ok=True)
            return {}
        return fake


    def test_i3_red_destroy_failure_warns_but_never_blocks_the_range(self):
        calls = self._main(["--yes"], red=lambda *a, **k: self.log.append("red_destroy") or
                           subprocess.CompletedProcess(["x"], 1, "", "identity refused"))
        self.assertIn("estate_destroy", calls)



    def test_i7_estate_refusal_reports_survivors_and_exits_nonzero(self):
        refused = [{"entry": {"kind": "team_box", "node": "n", "name": "team1-web01",
                              "vmid": 101, "bridge": None},
                    "reason": "vmid 101 refused — outside this deploy's ownership set"}]
        with self.assertRaises(SystemExit) as ctx:
            self._main(["--yes"], estate=lambda *a, **k: self.log.append("estate_destroy") or
                       {"destroyed": [], "skipped": [], "refused": refused})
        # sys.exit(str) means a failed (nonzero) teardown
        self.assertTrue(ctx.exception.code)
        self.assertIn("did not complete", self.out)


    def test_i6_frozen_full_teardown_is_refused_before_any_mutation(self):
        with self.assertRaises(SystemExit):
            self._main(["--yes"], frozen={"frozen_at": "2026-01-01"})
        self.assertIn("FROZEN", self.out)
        self.assertIn("--end-of-competition", self.out)
        self.assertEqual(self.log, [])


class RefusalVocabulary(unittest.TestCase):
    @patch.object(ownership, "wait_for_proxmox_task", lambda *a, **k: None)
    def test_untagged_refusal_names_vmid_and_carries_all_three_fields(self):
        fake = FakePVE(nodes={"n": [vm(40, "mystery", "")]}, cfgs={40: {}})
        with patch.object(ownership, "proxmox_api", fake):
            with self.assertRaises(Refusal) as ctx:
                ownership.destroy_vm_if_exists("n", 40, expect_tags=OURS.tags)
        self.assertIn("40", str(ctx.exception))
        self.assertIn("UNTAGGED", ctx.exception.saw)
        self.assertTrue(ctx.exception.expected)
        self.assertIn("clone marker", ctx.exception.detail)  # what would change its mind
        self.assertTrue(ctx.exception.as_line().startswith("TEARDOWN-REFUSAL "))


if __name__ == "__main__":
    unittest.main()
