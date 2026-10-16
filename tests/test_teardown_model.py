"""Unit tests for the teardown object model (docs/internals.md#teardown).

The characterization suite pins the OLD entry points; these pin the NEW
types: Ownership, Refusal's vocabulary and machine line, Inventory
classification and fail-closed reads, plan construction (evidence ordering as
a type check), and the engine's policy application + journal + verify_clean
exit contract."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import teardown  # noqa: E402
from estate import state as estate_state  # noqa: E402
from teardown.engine import TeardownEngine, load_teardown_journal  # noqa: E402
from teardown.inventory import Inventory  # noqa: E402
from teardown.plan import PlanError, TeardownPlan  # noqa: E402
from teardown.steps import (EVIDENCE, ArtifactStep, EngineCaptureStep, EstateStep,  # noqa: E402
                            Step, StepResult, TeardownContext)
from infra.ownership import Ownership, Refusal  # noqa: E402

RUN = "run-abcdef01"
OTHER_RUN = "run-12345678"
COMP = "c1"
OURS = Ownership(COMP, RUN)


class FakePVE:
    def __init__(self, nodes=None, cfgs=None, listing_fails=()):
        self.nodes = nodes or {}
        self.cfgs = cfgs or {}
        self.listing_fails = set(listing_fails)

    def __call__(self, method, path, **kw):
        if method == "GET" and path.endswith("/qemu"):
            node = path.split("/nodes/")[1].split("/")[0]
            if node in self.listing_fails:
                raise ConnectionError(f"{node} unreachable")
            return {"data": [dict(v) for v in self.nodes.get(node, [])]}
        if method == "GET" and path.endswith("/config"):
            return {"data": self.cfgs.get(int(path.split("/")[-2]), {})}
        if method == "GET" and path.endswith("/network"):
            return {"data": []}
        return {"data": None}


def vm(vmid, name, tags, status="stopped"):
    return {"vmid": vmid, "name": name, "tags": tags, "status": status}


class RefusalVocabulary(unittest.TestCase):
    def test_carries_saw_expected_remedy_and_renders_detail(self):
        r = Refusal(saw="vmid 9 with tags 'x'", expected="the full set",
                    remedy="none", detail="refusing: prose")
        self.assertEqual((r.saw, r.expected, r.remedy), ("vmid 9 with tags 'x'",
                                                         "the full set", "none"))
        self.assertEqual(str(r), "refusing: prose")

    def test_machine_line_is_the_bad_auto_contract_shape(self):
        r = Refusal(saw="s", expected="e", remedy="r")
        self.assertTrue(r.as_line().startswith("TEARDOWN-REFUSAL "))
        payload = json.loads(r.as_line().split(" ", 1)[1])
        self.assertEqual(payload, {"saw": "s", "expected": "e", "remedy": "r"})

    def test_is_a_runtime_error_so_existing_excepts_keep_catching(self):
        with self.assertRaises(RuntimeError):
            raise Refusal(saw="s", expected="e", remedy="r")


class InventoryClassified(unittest.TestCase):
    def test_owned_returns_only_full_set_matches_across_nodes(self):
        fake = FakePVE(nodes={
            "n1": [vm(1, "ours", f"tezcatlipoca;comp-{COMP};{RUN}"),
                   vm(2, "other-run", f"tezcatlipoca;comp-{COMP};{OTHER_RUN}")],
            "n2": [vm(3, "ours-sat", f"tezcatlipoca,comp-{COMP},{RUN}")],
        })
        inv = Inventory(["n1", "n2"], api=fake)
        got = [(n, v["vmid"]) for n, v in inv.owned(OURS)]
        self.assertEqual(sorted(got), [("n1", 1), ("n2", 3)])

    def test_listing_failure_fails_closed(self):
        fake = FakePVE(nodes={"n1": [vm(1, "x", "")]}, listing_fails=["n2"])
        inv = Inventory(["n1", "n2"], api=fake)
        self.assertEqual(inv.listing_failed()[0][0], "n2")

    def test_invalidate_forces_a_fresh_listing(self):
        fake = FakePVE(nodes={"n1": [vm(1, "x", "")]})
        inv = Inventory(["n1"], api=fake)
        inv.view("n1")
        fake.nodes["n1"].append(vm(2, "y", ""))
        self.assertEqual(len(inv.view("n1").vms), 1)      # cached within a generation
        inv.invalidate()
        self.assertEqual(len(inv.view("n1").vms), 2)      # fresh after invalidate


class NoopStep(Step):
    name = "noop"
    kind = EVIDENCE

    def run(self, ctx):
        return StepResult(self.name, "ok")


class PlanConstruction(unittest.TestCase):
    def _terminal(self, steps):
        return TeardownPlan([], steps)

    def test_evidence_after_destruction_cannot_be_built(self):
        with self.assertRaises(PlanError) as ctx:
            self._terminal([EstateStep(), ArtifactStep()])
        self.assertIn("evidence-before-destruction", str(ctx.exception))

    def test_the_shipped_order_builds(self):
        from teardown.modes import build_terminal_plan
        plan = build_terminal_plan()
        names = [s.name for s in plan.steps]
        self.assertEqual(names, ["portal", "engine_capture", "artifacts", "red01", "pre_stop",
                                 "estate", "remote_access"])


class EnginePoliciesAndJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.comp = Path(self.tmp.name) / "competitions" / "c1"
        self.comp.mkdir(parents=True)
        self.inv = Inventory(["n"], api=FakePVE(nodes={"n": []}))
        self.ctx = TeardownContext(
            comp_dir=self.comp, env={}, teams={}, boxes=[],
            deployed_state={"run_id": RUN}, ownership=OURS, placement=None,
            nodes=["n"], default_node="n", inventory=self.inv)
        self.lines = []

    def _engine(self):
        return TeardownEngine(self.ctx, log=lambda s: self.lines.append(s))

    def _plan(self, steps):
        return TeardownPlan([], steps or [NoopStep()])

    def test_warn_policy_continues_and_never_journals_done(self):
        class Boom(Step):
            name = "boom"
            kind = EVIDENCE

            def run(self, ctx):
                raise RuntimeError("estate on fire")

        results = self._engine().execute(self._plan([Boom()]))
        self.assertEqual(results[0].status, "warned")
        journal = load_teardown_journal(self.comp)
        self.assertNotIn("boom", journal.get("done") or {},
                         "a warned step failed — a re-run must retry it")

    def test_refuse_policy_stops_the_plan(self):
        ran = []

        class Refuser(Step):
            name = "refuser"
            kind = EVIDENCE
            failure = teardown.REFUSE

            def run(self, ctx):
                raise RuntimeError("hard stop")

        class After(Step):
            name = "after"
            kind = EVIDENCE

            def run(self, ctx):
                ran.append(True)
                return StepResult(self.name, "ok")

        results = self._engine().execute(self._plan([Refuser(), After()]))
        self.assertEqual(results[0].status, "failed")
        self.assertEqual(ran, [])

    def test_resumable_step_is_skipped_when_journalled(self):
        calls = []

        class Collect(Step):
            name = "artifacts"
            kind = EVIDENCE
            resumable = True

            def run(self, ctx):
                calls.append(1)
                return StepResult(self.name, "ok", "/tmp/test-dir")

        eng = self._engine()
        eng.execute(self._plan([Collect()]))
        eng.execute(self._plan([Collect()]))
        self.assertEqual(len(calls), 1)   # second pass skipped via the journal

    def test_gates_run_before_any_step(self):
        order = []

        def gate(ctx):
            order.append("gate")
            raise Refusal(saw="s", expected="e", remedy="r")

        class Never(Step):
            name = "never"
            kind = EVIDENCE

            def run(self, ctx):
                order.append("never")
                return StepResult(self.name, "ok")

        with self.assertRaises(Refusal):
            self._engine().execute(TeardownPlan([gate], [Never()]))
        self.assertEqual(order, ["gate"])


class VerifyClean(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.comp = Path(self.tmp.name) / "c1"
        self.comp.mkdir(parents=True)
        self.fake = FakePVE(nodes={"n": []})
        self.inv = Inventory(["n"], api=self.fake)
        self.ctx = TeardownContext(
            comp_dir=self.comp, env={}, teams={}, boxes=[],
            deployed_state={"run_id": RUN}, ownership=OURS, placement=None,
            nodes=["n"], default_node="n", inventory=self.inv, full=True)

    def _engine(self):
        return TeardownEngine(self.ctx, log=lambda s: None)

    def _plan(self):
        from teardown.modes import build_terminal_plan
        return build_terminal_plan()

    def test_full_mode_untouched_estate_is_clean(self):
        report = self._engine().verify_clean(self._plan())
        self.assertTrue(report.clean, report.problems)

    def test_our_surviving_vm_is_a_problem_in_full_mode(self):
        self.fake.nodes["n"].append(vm(5, "team1-web01",
                                       f"tezcatlipoca;comp-{COMP};{RUN}", status="running"))
        report = self._engine().verify_clean(self._plan())
        self.assertFalse(report.clean)
        self.assertTrue(any("5" in p for p in report.problems))

    def test_different_run_survivor_is_a_note_never_a_problem(self):
        self.fake.nodes["n"].append(vm(6, "team1-web01",
                                       f"tezcatlipoca;comp-{COMP};{OTHER_RUN}",
                                       status="running"))
        report = self._engine().verify_clean(self._plan())
        self.assertTrue(report.clean, report.problems)   # concurrent coexistence
        self.assertTrue(any("DIFFERENT run" in n for n in report.notes))

    def test_a_recycled_red01_vmid_is_attributed_and_not_this_ranges_leak(self):
        # The occupant's bad-auto marker names another competition, so OUR red01 is gone and
        # the vmid was recycled — a note, not a refusal.
        (self.comp / ".badauto-config.yaml").write_text("deploy: {red_vmid: 3570}\n")
        self.ctx.deployed_state = {"run_id": RUN, "assume_breach": {"vmid": 3570}}

        def api(method, path, **kw):
            if path == "/cluster/resources":
                return {"data": [{"vmid": 3570, "node": "n", "name": "bad-auto-red01",
                                  "status": "running"}]}
            return {"data": {"description": "bad-auto red01 comp=other-range id=deadbeef"}}

        with patch("infra.red01.proxmox_api", side_effect=api):
            report = self._engine().verify_clean(self._plan())
        self.assertTrue(report.clean, report.problems)
        self.assertTrue(any("another competition's red01" in n for n in report.notes))

    def test_an_unmarked_red01_occupant_is_fail_closed(self):
        (self.comp / ".badauto-config.yaml").write_text("deploy: {red_vmid: 3570}\n")
        self.ctx.deployed_state = {"run_id": RUN, "assume_breach": {"vmid": 3570}}

        def api(method, path, **kw):
            if path == "/cluster/resources":
                return {"data": [{"vmid": 3570, "node": "n", "name": "some-vm",
                                  "status": "running"}]}
            return {"data": {"description": ""}}

        with patch("infra.red01.proxmox_api", side_effect=api):
            report = self._engine().verify_clean(self._plan())
        self.assertFalse(report.clean)
        self.assertTrue(any("no bad-auto marker" in p for p in report.problems))

    def test_unlistable_node_is_not_clean(self):
        self.fake.listing_fails.add("n")
        report = self._engine().verify_clean(self._plan())
        self.assertFalse(report.clean)
        self.assertTrue(any("unverifiable" in p for p in report.problems))

    def test_teams_only_keeps_stopped_templates_by_design(self):
        self.ctx.full = False
        self.fake.nodes["n"].append(
            vm(7, "golden-web01",
               f"tezcatlipoca;comp-{COMP};{RUN};tezcatlipoca-golden", status="stopped"))
        report = self._engine().verify_clean(self._plan())
        self.assertTrue(report.clean, report.problems)
        self.assertTrue(any("kept" in n for n in report.notes))

    def test_teams_only_never_reaches_the_template_destroy(self):
        # The scrim harness's --keep-templates sets exactly this (ctx.full=False), which is
        # what makes the templates survive with their hash record for the next run.
        from engine.template_freeze import save_template_hashes
        save_template_hashes(self.comp, golden={"web01": {"hash": "h"}})
        ctx = TeardownContext(comp_dir=self.comp, env={}, teams={}, boxes=[], deployed_state={},
                              ownership=OURS, placement=None, nodes=["n"], default_node="n",
                              inventory=None, full=False)
        with patch("teardown.machinery.sweep_tagged_leftovers"), \
                patch("teardown.machinery.teardown_templates", return_value=[]) as destroy:
            result = EstateStep().run(ctx)
        destroy.assert_not_called()
        self.assertEqual(result.status, "ok")
        from engine.template_freeze import load_template_hashes
        # reuse record kept with them
        self.assertTrue(load_template_hashes(self.comp).get("golden"))

    def test_full_teardown_does_reach_the_template_destroy(self):
        # The other half of the pair: a default teardown still removes the templates.
        ctx = TeardownContext(comp_dir=self.comp, env={}, teams={}, boxes=[], deployed_state={},
                              ownership=OURS, placement=None, nodes=["n"], default_node="n",
                              inventory=None, full=True)
        with patch("teardown.machinery.sweep_tagged_leftovers"), \
                patch("teardown.machinery.teardown_templates", return_value=[]) as destroy:
            result = EstateStep().run(ctx)
        self.assertTrue(destroy.call_args.kwargs["full"])
        self.assertEqual(result.status, "ok")

    def test_lost_evidence_is_a_note_never_a_problem(self):
        # A teardown that lost evidence still exits clean — a wedged box never holds a destroy
        # hostage — but it must not read like a clean one at the point the operator reads it.
        from teardown.engine import save_teardown_journal
        test_dir = self.comp / ".automated-tests" / RUN
        test_dir.mkdir(parents=True)
        (test_dir / "collection.json").write_text(json.dumps({"targets": [{"name": "red01", "files": [
            {"want": "/tmp/ba/world.json", "status": "unreachable"}]}]}))
        save_teardown_journal(self.comp, {"run_id": RUN, "done": {"artifacts": {
            "detail": str(test_dir), "test_dir": str(test_dir)}}})
        report = self._engine().verify_clean(self._plan())
        self.assertTrue(report.clean, report.problems)
        self.assertTrue(any("evidence incomplete" in n and "red01" in n for n in report.notes),
                        report.notes)


class EngineCapture(unittest.TestCase):
# The engine's final scoreboard lives only in its database, and EstateStep deletes that
#     database: the step must capture it for the run whose harness never did — and never
#     overwrite a harness capture that already exists.

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.comp = Path(self.tmp.name) / "c1"
        self.comp.mkdir(parents=True)
        self.ctx = TeardownContext(
            comp_dir=self.comp, env={}, teams={"team1": {"identifier": "101"}},
            boxes=[{"name": "dc01"}],
            deployed_state={"run_id": RUN, "engine_mgmt_ip": "10.0.0.9",
                            "scoring_password": "s"},
            ownership=OURS, placement=None, nodes=["n"], default_node="n", inventory=None)

    def _folder(self):
        from artifacts_lib.lifecycle import latest_run_key
        from artifacts_lib.paths import test_dir
        key, _ = latest_run_key(self.comp, RUN)
        return test_dir(self.comp, key)

    def test_it_captures_and_records_the_engine_folder(self):
        seen = {}

        def fake_dump(base_url, session, comp_dir, *, out_dir=None):
            seen.update(base_url=base_url, comp_dir=comp_dir, out_dir=Path(out_dir))
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            (Path(out_dir) / "event-final-20260101-000000.json").write_text("{}")
            return []

        with patch("quotient.event_capture.login_scoring", return_value=object()), \
                patch("quotient.event_capture.dump_scoreboard", side_effect=fake_dump):
            result = EngineCaptureStep().run(self.ctx)
        from artifacts_lib.manifest import load_manifest
        self.assertEqual(result.status, "ok")
        self.assertEqual(seen["base_url"], "http://10.0.0.9")
        # A source the collector only reads — never its own target, whose local seal unlinks
        # the destination before copying it.
        self.assertEqual(seen["out_dir"], self.comp / "evidence")
        self.assertEqual(load_manifest(self._folder())["paths"]["engine_evidence"],
                         str(self.comp / "evidence"))

    def test_an_empty_harness_folder_is_replaced_not_captured_into(self):
        # A recorded-but-empty engine dir means the harness capture never landed. Capturing
        # into it would be a self-copy: the collector's local seal unlinks the destination
        # before copying, so the file it just wrote would be gone and recorded absent.
        from artifacts_lib.manifest import load_manifest, record_paths
        stale = self._folder() / "evidence" / "engine"
        stale.mkdir(parents=True)
        record_paths(self._folder(), engine_evidence=stale)
        with patch("quotient.event_capture.login_scoring", return_value=object()), \
                patch("quotient.event_capture.dump_scoreboard", return_value=[]) as dump:
            result = EngineCaptureStep().run(self.ctx)
        self.assertEqual(result.status, "ok")
        self.assertEqual(Path(dump.call_args.kwargs["out_dir"]), self.comp / "evidence")
        self.assertEqual(load_manifest(self._folder())["paths"]["engine_evidence"],
                         str(self.comp / "evidence"))

    def test_a_present_harness_capture_is_not_overwritten(self):
        from artifacts_lib.manifest import record_paths
        ev = self._folder() / "evidence" / "engine"
        ev.mkdir(parents=True)
        (ev / "final-scoreboard.json").write_text("{}")
        record_paths(self._folder(), engine_evidence=ev)
        with patch("quotient.event_capture.dump_scoreboard") as dump, \
                patch("quotient.event_capture.login_scoring") as login:
            result = EngineCaptureStep().run(self.ctx)
        self.assertEqual(result.status, "skipped")
        dump.assert_not_called()
        login.assert_not_called()

    def test_without_an_engine_address_it_does_nothing(self):
        self.ctx.deployed_state = {"run_id": RUN}
        with patch("quotient.event_capture.dump_scoreboard") as dump:
            result = EngineCaptureStep().run(self.ctx)
        self.assertEqual(result.status, "ok")
        self.assertIn("no engine management IP", result.detail)
        dump.assert_not_called()

    def test_without_a_scoring_password_it_warns_and_does_not_dial(self):
        self.ctx.deployed_state = {"run_id": RUN, "engine_mgmt_ip": "10.0.0.9"}
        with patch("quotient.event_capture.login_scoring") as login:
            result = EngineCaptureStep().run(self.ctx)
        self.assertEqual(result.status, "warned")
        login.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class RedTargetVmid(unittest.TestCase):
# RedStep records the vmid badauto's own target line names — not the
#     shared config.yaml's (another session's deploy rewrites it mid-flight;
#     live-found 2026-10-12: verify_clean then verified the WRONG vmid).

    def test_parses_the_target_line(self):
        from teardown.steps import _red_target_vmid
        proc = SimpleNamespace(stdout="destroy target: competition_dir=/x red01=10.0.0.199 "
                                        "(vmid 999) engine=10.0.0.253\nmore", stderr="")
        self.assertEqual(_red_target_vmid(proc), 999)

    def test_no_target_line_is_none(self):
        from teardown.steps import _red_target_vmid
        self.assertIsNone(_red_target_vmid(SimpleNamespace(stdout="", stderr="")))


class JournalLifecycle(unittest.TestCase):
# A FRESH deploy starts a new teardown cycle: phase-1 clears the previous
#     teardown's journal (a reused run id would otherwise skip red01/artifacts of
#     the new range — live-found 2026-10-12). Resumes keep it.

    def test_phase1_clears_a_stale_journal(self):
        import tempfile
        from types import SimpleNamespace
        from deploy_lib.phases import cleanup
        with tempfile.TemporaryDirectory() as d:
            comp = Path(d)
            document = estate_state.EstateState.load(comp)
            document.section("teardown")["journal"] = {"run_id": "run-x",
                                                       "done": {"red01": {}}}
            document.save()
            ctx = SimpleNamespace(
                from_phase=1, comp_dir=comp, node="n", all_targets=[], engine_vmid=1000,
                boxes=[], reclaim_tags=set(), placement=None, teams={"team1": {"identifier": "101"}},
                golden_hashes={}, comp_name="c1", reclaim_tag="run-x",
                run_id="run-x")
            with patch.object(cleanup, "proxmox_api", return_value={"data": []}):
                cleanup.phase1_cleanup(ctx)
            self.assertEqual(
                estate_state.EstateState.load(comp).section("teardown"), {},
                "a fresh deploy must drop the previous teardown's journal")

    def test_phase1_skip_on_resume_keeps_the_journal(self):
        import tempfile
        from types import SimpleNamespace
        from deploy_lib.phases import cleanup
        with tempfile.TemporaryDirectory() as d:
            comp = Path(d)
            document = estate_state.EstateState.load(comp)
            document.section("teardown")["journal"] = {"run_id": "run-x",
                                                       "done": {"red01": {}}}
            document.save()
            ctx = SimpleNamespace(from_phase=2, comp_dir=comp)
            cleanup.phase1_cleanup(ctx)   # early return, nothing touched
            self.assertTrue(
                estate_state.EstateState.load(comp).section("teardown").get("journal"))
