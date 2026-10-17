"""Per-phase unit tests for the extracted deploy phases (deploy_lib/phases/).

Only the decidable parts are tested — the branching, ordering and state writes each phase
owns. Every infrastructure call is patched: none of these tests touches Proxmox, SSH or
the network. They are the offline half of "the phase boundary is a call boundary"; the
byte-identity of the shell/config these phases generate is covered by the modules that own
that generation.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import deploy  # noqa: E402
from config import injects_config  # noqa: E402
from _deploy_patch import dpatch  # noqa: E402
from deploy_lib.phases import cleanup as dl_cleanup  # noqa: E402
from deploy_lib.phases import engine as dl_engine  # noqa: E402
from deploy_lib.phases import golden as dl_golden  # noqa: E402
from deploy_lib.phases import postclone as dl_postclone  # noqa: E402
from deploy_lib.phases import seed as dl_seed  # noqa: E402
from estate import state as estate_state  # noqa: E402
from infra.ownership import MARKER, MISSING, OWNED, UNPROVEN  # noqa: E402
from nakonops.nakon_run_ops import NakonResult  # noqa: E402


def _ctx(comp_dir, **overrides):
# A DeployContext with harmless defaults; override what a test exercises.
    fields = dict(
        comp_dir=comp_dir, comp_name=comp_dir.name,
        state_path=comp_dir / "estate-state.json", from_phase=1, resuming=False,
        assume_yes=True, name="probe", scenario="probe", box_username="ubuntu",
        credlist_usernames=["admin"], nakon_jobs=4, apt_cache=True, injects=[],
        packet_pw=None, state={}, teams={}, number_of_teams=0, admin_password="a",
        scoring_password="s", postgres_password="p", redis_password="r", box_password="b", box_creds={},
        domain_creds=None, inject_password=None, placement=None, node="pve",
        engine_vmid=1000, engine_mgmt_ip="10.0.0.1", boxes=[], boxes_by_name={},
        unbooted=set(), nakon_config_path=comp_dir / "nakon.json",
        golden_config_path=comp_dir / "golden.json",
        predomain_config_path=comp_dir / "predomain.json",
        final_config_path=comp_dir / "final.json",
        golden_inputs={}, golden_hashes={},
 ssh_key_abs="/k",
        all_targets=[], managed_targets=[], linux_targets=[], windows_targets=[],
        tf_ctx={"ssh_key_path": "/k", "scoring_engine_ip": "10.0.0.99",
                "vm_username": "ubuntu"},
        ssh_key=Path("/k"), scoring_user="ubuntu", scoring_ip="10.0.0.99",
    )
    fields.update(overrides)
    return deploy.DeployContext(**fields)


def _nulltimed():
    return dpatch("timed",
                        side_effect=lambda *a, **k: contextlib.nullcontext())


class Phase1Cleanup(unittest.TestCase):
    """Phase 1 no longer tears a range down: it reclaims the untagged interrupted clones
    (infra.ownership.MARKER) at this deploy's own slots and resets the comp dir. Every VM a
    run owns is preflight's business (infra.collisions refusal + destroy-competition.py)."""

    def _run(self, ctx, vms=(), verdicts=None):
        calls = types.SimpleNamespace(destroyed=[], markers=[])
        verdicts = verdicts or {}

        def fake_verdict(node, vmid, tags, expect):
            return verdicts.get(vmid, UNPROVEN), set()

        with dpatch("proxmox_api", return_value={"data": list(vms)}), \
                dpatch("ownership_verdict", side_effect=fake_verdict), \
                dpatch("destroy_vm_if_exists",
                       side_effect=lambda node, vmid, **k: calls.destroyed.append(vmid)), \
                dpatch("reset_domain_markers",
                       side_effect=lambda c: calls.markers.append(c)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            dl_cleanup.phase1_cleanup(ctx)
        return calls, out.getvalue()

    def _comp(self, d, **overrides):
        target = {"vmid": 201, "vm_name": "web01-team101", "node": "pve"}
        return _ctx(Path(d), teams={"team1": {"identifier": "101", "password": "x"}},
                    all_targets=[target], **overrides)

    def test_fresh_run_reclaims_an_interrupted_clone_and_resets_local_state(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            (comp_dir / ".postclone-swept").write_text("stale")
            (comp_dir / ".nakon-domain-team1-adds.json").write_text("{}")
            ctx = self._comp(d)
            calls, out = self._run(ctx, vms=[{"vmid": 201, "name": "web01-team101"}],
                                   verdicts={201: MARKER})
            self.assertEqual(calls.destroyed, [201])
            self.assertEqual(calls.markers, [comp_dir])
            self.assertFalse((comp_dir / ".postclone-swept").exists())
        self.assertIn("reclaiming", out)

    def test_a_range_leftover_is_never_destroyed_here(self):
        # OWNED (this comp dir's own run) and MISSING (another worktree's) are both a
        # leftover RANGE: preflight refuses the deploy, and phase 1 must not touch it.
        for verdict in (OWNED, MISSING):
            with self.subTest(verdict=verdict), tempfile.TemporaryDirectory() as d:
                calls, _out = self._run(self._comp(d),
                                        vms=[{"vmid": 201, "name": "web01-team101"}],
                                        verdicts={201: verdict})
                self.assertEqual(calls.destroyed, [])

    def test_a_vmid_outside_this_deploys_slots_is_ignored(self):
        # No node sweep: a marker VM at a vmid this deploy does not use is not ours to
        # reclaim here (a full teardown owns the competition's whole span).
        with tempfile.TemporaryDirectory() as d:
            calls, _out = self._run(self._comp(d),
                                    vms=[{"vmid": 9999, "name": "someone-elses"}],
                                    verdicts={9999: MARKER})
        self.assertEqual(calls.destroyed, [])

    def test_unreadable_node_is_a_warning_not_a_failure(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = self._comp(d)
            with dpatch("proxmox_api", side_effect=RuntimeError("api down")), \
                    dpatch("reset_domain_markers"), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                dl_cleanup.phase1_cleanup(ctx)
        self.assertIn("could not scan", out.getvalue())

    def test_resume_touches_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            (comp_dir / ".postclone-swept").write_text("keep me")
            ctx = self._comp(d, from_phase=2)
            with dpatch("proxmox_api") as p_api, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                dl_cleanup.phase1_cleanup(ctx)
            p_api.assert_not_called()
            self.assertEqual((comp_dir / ".postclone-swept").read_text(), "keep me")
        self.assertIn("Skipped (resume)", out.getvalue())


class Phase3Order(unittest.TestCase):
    def test_engine_prep_precedes_event_conf_precedes_nat(self):
        with tempfile.TemporaryDirectory() as d:
            order = []
            ctx = _ctx(Path(d))
            with dpatch("prepare_engine_from_template",
                              side_effect=lambda *a: order.append("template")), \
                    dpatch("push_event_conf",
                                 side_effect=lambda *a, **k: order.append("event_conf")), \
                    dpatch("ensure_nat_forwarding",
                                 side_effect=lambda *a: order.append("nat")), \
                    patch("net.remote_access_ops.setup_remote_access",
                          side_effect=lambda *a: order.append("remote_access")), \
                    contextlib.redirect_stdout(io.StringIO()):
                dl_engine.phase3_prepare_engine(ctx)
        self.assertEqual(order, ["template", "event_conf", "nat", "remote_access"])

    def test_remote_access_disabled_flag_skips_the_step(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            (comp_dir / "Compfile").write_text("remote_access 0\n")
            ctx = _ctx(comp_dir)
            with dpatch("prepare_engine_from_template"), \
                    dpatch("push_event_conf"), \
                    dpatch("ensure_nat_forwarding"), \
                    patch("net.remote_access_ops.setup_remote_access") as p_ra, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                dl_engine.phase3_prepare_engine(ctx)
            p_ra.assert_not_called()
            self.assertIn("remote_access 0", out.getvalue())


class Phase6PredomainPass(unittest.TestCase):
    def test_resume_marker_skips_the_whole_sweep(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            marker = comp_dir / ".postclone-swept"
            marker.write_text("done")
            ctx = _ctx(comp_dir)
            with dpatch("run_nakon") as p_nakon, \
                    dpatch("fix_services_on_boxes") as p_fix, \
                    dpatch("ensure_nat_forwarding") as p_nat, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                dl_postclone.phase6_predomain_pass(ctx)
            self.assertIn("[6/8] Resume marker present", out.getvalue())
            p_nakon.assert_not_called()
            p_fix.assert_not_called()
            p_nat.assert_not_called()
            self.assertEqual(marker.read_text(), "done")

    def test_no_predomain_machines_still_runs_fix_services_and_writes_marker(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            ctx = _ctx(comp_dir)
            ctx.predomain_config_path.write_text(json.dumps({"machines": []}))
            with dpatch("run_nakon") as p_nakon, \
                    dpatch("fix_services_on_boxes") as p_fix, \
                    dpatch("ensure_nat_forwarding"), \
                    dpatch("compfile_flag", return_value=0), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                dl_postclone.phase6_predomain_pass(ctx)
            self.assertIn("No pre-domain configurations in this lineup", out.getvalue())
            p_nakon.assert_not_called()
            p_fix.assert_called_once()
            self.assertTrue((comp_dir / ".postclone-swept").exists())

    def test_a_lineup_with_no_postclone_configs_records_a_clean_verdict(self):
    # caught live: a lineup whose configs are ALL golden-stage
    #         (same-type-2box) never reached record_stage_coverage, so
    #         `plant_coverage_failed` was never created and verify's coverage gate failed
    #         closed on a completely clean run — "coverage was never recorded (pre-tally
    #         deploy?)". "Nothing to plant post-clone" is a clean result, not an absent one.
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            ctx = _ctx(comp_dir)
            ctx.predomain_config_path.write_text(json.dumps({"machines": []}))
            with dpatch("run_nakon"), \
                    dpatch("fix_services_on_boxes"), \
                    dpatch("ensure_nat_forwarding"), \
                    dpatch("compfile_flag", return_value=0), \
                    contextlib.redirect_stdout(io.StringIO()):
                dl_postclone.phase6_predomain_pass(ctx)
        self.assertIn("plant_coverage_failed", ctx.state)
        self.assertEqual(ctx.state["plant_coverage_failed"], {})
        self.assertEqual(ctx.state["nakon_failed_steps"], [])

    def test_predomain_pass_records_the_stage_tally_and_persists_coverage(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            machines = [{"name": "web01-team101", "configurations": []}]
            ctx = _ctx(comp_dir)
            ctx.predomain_config_path.write_text(json.dumps({"machines": machines}))
            result = NakonResult(["sshd-hardening"],
                                 [{"name": "web01-team101", "steps": []}])
            with dpatch("run_nakon", return_value=result) as p_nakon, \
                    dpatch("build_nakon_bundle", return_value="bundle"), \
                    dpatch("fix_services_on_boxes"), \
                    dpatch("ensure_nat_forwarding"), \
                    dpatch("compfile_flag", return_value=0), \
                    dpatch("record_stage_coverage"), \
                    contextlib.redirect_stdout(io.StringIO()):
                dl_postclone.phase6_predomain_pass(ctx)
            self.assertEqual(ctx.state["nakon_failed_steps"], ["predomain: sshd-hardening"])
            _args = p_nakon.call_args
            self.assertIs(_args.kwargs["strict"], False)
            self.assertEqual(_args.kwargs["jobs"], 4)


class Phase4GoldenCoverage(unittest.TestCase):
# Phase 4 records the golden plant's coverage verdict like a post-clone stage's:
#     the closure threaded into build_golden_set writes plant_coverage_failed (and the
#     stage-prefixed tally on tolerated failures) and persists through ctx.save_state.
#     The integration halves join here with the REAL check_plant_coverage — a golden-only
#     lineup (same-type-2box shape) must come out PASS on a clean plant and FAIL, with
#     the (golden-stage) annotation, on an alpine-tolerated one.

    def _run_phase4(self, comp_dir):
    # Run phase4_golden_set with build_golden_set faked; returns (ctx, coverage).
        captured = []
        ctx = _ctx(comp_dir, golden_hashes={}, golden_inputs={})
        with dpatch("load_template_hashes", return_value={}), \
                dpatch("golden_rebuild_gate"), \
                dpatch("build_golden_set",
                             side_effect=lambda *a, **k:
                                 (captured.append(k["coverage"]) or {"web01": 1250})), \
                dpatch("_template_vmid_map", return_value={}), \
                dpatch("write_text_atomic"), \
                dpatch("create_team_boxes"), \
                dpatch("run_concurrent", return_value=[]), \
                dpatch("wait_for_boxes_ssh"), \
                dpatch("wait_for_cloud_init"), \
                dpatch("fix_dns_on_boxes"), \
                dpatch("prep_apt_on_boxes"), \
                dpatch("snap_base"), \
                _nulltimed(), \
                contextlib.redirect_stdout(io.StringIO()):
            dl_golden.phase4_golden_set(ctx)
        return ctx, captured[0]

    @staticmethod
    def _gate(comp_dir, machines):
    # The real plant-coverage gate against a real comp dir.
    #         Returns (gate, stdout); status compared by NAME — this helper loads its own
    #         module copy, so the gate's Status enum is not the caller's object.
        (comp_dir / "nakon-config.json").write_text(
            json.dumps({"machines": machines}))
        import verifier.state_gates as v_state_gates  # noqa: E402
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gate = v_state_gates.check_plant_coverage(comp_dir)
        return gate, out.getvalue()

    def test_the_closure_is_threaded_and_a_clean_plant_records_pass(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            ctx, coverage = self._run_phase4(comp_dir)
            self.assertIsNotNone(coverage)
            machines = [{"name": "web01-golden", "configurations": ["apache"]}]
            clean = NakonResult([], [{"name": "web01-golden",
                                      "steps": [{"name": "apache", "rc": 0}]}])
            coverage(machines, clean)
            self.assertEqual(ctx.state["plant_coverage_failed"], {})
            on_disk = estate_state.load_flat(comp_dir)
            self.assertEqual(on_disk["plant_coverage_failed"], {})
            gate, out = self._gate(comp_dir, [
                {"name": "web01-team122", "configurations": ["apache"]}])
        self.assertEqual(gate.status.name, "PASS")
        self.assertIn("full config coverage", out)

    def test_a_tolerated_golden_failure_fails_the_real_gate(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            ctx, coverage = self._run_phase4(comp_dir)
            machines = [{"name": "web01-golden", "configurations": ["apache", "bind"]}]
            tolerated = NakonResult(
                ["box0: apache rc=1 (FAILED)"],
                [{"name": "web01-golden", "steps": [{"name": "apache", "rc": 1},
                                                    {"name": "bind", "rc": 0}]}])
            coverage(machines, tolerated)
            self.assertEqual(ctx.state["plant_coverage_failed"],
                             {"web01-golden": ["apache"]})
            self.assertEqual(ctx.state["nakon_failed_steps"],
                             ["golden: box0: apache rc=1 (FAILED)"])
            gate, out = self._gate(comp_dir, [
                {"name": "web01-team122", "configurations": ["apache"]}])
        self.assertEqual(gate.status.name, "FAIL")
        self.assertIn("apache (golden-stage)", out)

    def test_a_skip_verdict_never_erases_a_recorded_failure(self):
    # result=None (reuse / checkpoint / pristine / cold): the record must come
    #         into existence if absent, but an already-recorded failure survives — the
    #         disk is proven by hash, not by a fresh plant.
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            ctx, coverage = self._run_phase4(comp_dir)
            ctx.state["plant_coverage_failed"] = {"web01-golden": ["apache"]}
            coverage([{"name": "web01-golden", "configurations": ["apache"]}], None)
            self.assertEqual(ctx.state["plant_coverage_failed"],
                             {"web01-golden": ["apache"]})
            gate, _out = self._gate(comp_dir, [
                {"name": "web01-team122", "configurations": ["apache"]}])
        self.assertEqual(gate.status.name, "FAIL")


class Phase7Seed(unittest.TestCase):
    def _run(self, ctx):
        order = []
        with dpatch("wait_for_http",
                          side_effect=lambda *a, **k: order.append("http")), \
                dpatch("seed_teams",
                             side_effect=lambda *a, **k: order.append("seed")), \
                dpatch("engine_paused", return_value=True), \
                dpatch("unpause_engine",
                             side_effect=lambda *a: order.append("unpause")), \
                dpatch("resolve_inject_times",
                             side_effect=lambda *a: order.append("resolve")), \
                dpatch("create_injects",
                             side_effect=lambda *a: order.append("injects")
                             or ([], [])), \
                contextlib.redirect_stdout(io.StringIO()):
            dl_seed.phase8_seed(ctx)
        return order

    def test_fresh_seed_then_unpause_then_injects(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), state={}, injects=[{"title": "i1"}])
            order = self._run(ctx)
        self.assertEqual(order, ["http", "seed", "unpause", "resolve", "injects"])
        self.assertTrue(ctx.state["seeded"])
        self.assertTrue(ctx.state["engine_unpaused"])
        self.assertTrue(ctx.state["injects_created"])

    def test_seeded_flag_short_circuits_the_seed_post(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), state={"seeded": True})
            order = self._run(ctx)
        self.assertNotIn("seed", order)
        self.assertEqual(order, ["http", "unpause"])

    def test_engine_already_unpaused_is_recorded_without_a_post(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), state={})
            with dpatch("wait_for_http"), \
                    dpatch("seed_teams"), \
                    dpatch("engine_paused", return_value=False), \
                    dpatch("unpause_engine") as p_unpause, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                dl_seed.phase8_seed(ctx)
        p_unpause.assert_not_called()
        self.assertIn("Engine reports itself unpaused", out.getvalue())
        self.assertTrue(ctx.state["engine_unpaused"])

    def test_hold_start_seeds_without_starting_and_defers_injects(self):
    # --hold-start: teams are seeded (identifiers posted, clock NOT started), the
    #         engine stays paused and injects are deferred to competition-ops start —
    #         their windows anchor at creation time, so creating them here would
    #         open them before T0.
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), state={"hold_start": True}, injects=[{"title": "i1"}])
            with dpatch("wait_for_http",
                              side_effect=lambda *a, **k: None), \
                    dpatch("seed_teams", return_value=None) as p_seed, \
                    dpatch("engine_paused", return_value=True), \
                    dpatch("unpause_engine") as p_unpause, \
                    dpatch("resolve_inject_times") as p_resolve, \
                    dpatch("create_injects") as p_create, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                dl_seed.phase8_seed(ctx)
        p_seed.assert_called_once()
        self.assertIs(p_seed.call_args.kwargs.get("start"), False)
        p_unpause.assert_not_called()
        p_resolve.assert_not_called()
        p_create.assert_not_called()
        self.assertTrue(ctx.state["seeded"])
        self.assertNotIn("engine_unpaused", ctx.state)
        self.assertNotIn("injects_created", ctx.state)
        self.assertIn("Start is HELD", out.getvalue())

    def test_a_held_resume_stays_held_and_posts_nothing(self):
    # The hold lives in the state file, so --from-phase 8 over a held state re-runs
    #         phase 8 without starting anything.
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), state={"hold_start": True, "seeded": True},
                       injects=[{"title": "i1"}])
            with dpatch("wait_for_http"), \
                    dpatch("seed_teams") as p_seed, \
                    dpatch("engine_paused") as p_paused, \
                    dpatch("unpause_engine") as p_unpause, \
                    dpatch("create_injects") as p_create, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                dl_seed.phase8_seed(ctx)
        p_seed.assert_not_called()
        p_paused.assert_not_called()
        p_unpause.assert_not_called()
        p_create.assert_not_called()
        self.assertIn("Start is HELD", out.getvalue())

    def test_failed_injects_are_not_marked_created(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), state={"seeded": True, "engine_unpaused": True},
                       injects=[{"title": "i1"}])
            with dpatch("wait_for_http"), \
                    dpatch("engine_paused", return_value=False), \
                    dpatch("resolve_inject_times"), \
                    dpatch("create_injects",
                                 return_value=([], ["Bad inject"])), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                dl_seed.phase8_seed(ctx)
        self.assertNotIn("injects_created", ctx.state)
        self.assertIn("1 inject(s) failed to create: Bad inject", out.getvalue())

    def test_an_inject_added_after_the_first_run_is_not_skipped(self):
    # The bug: `injects_created` was a bare boolean, so a new inject was never
    #         posted on a resume — create_injects dedups on titles, but nothing re-ran it.
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), state={"seeded": True, "engine_unpaused": True},
                       injects=[{"title": "i1"}, {"title": "i2"}])
            stale = injects_config.injects_fingerprint([{"title": "i1"}])
            ctx.state["injects_created"] = True
            ctx.state["injects_fingerprint"] = stale
            with dpatch("wait_for_http"), \
                    dpatch("engine_paused", return_value=False), \
                    dpatch("resolve_inject_times"), \
                    dpatch("create_injects",
                                 return_value=([], [])) as p_create, \
                    contextlib.redirect_stdout(io.StringIO()):
                dl_seed.phase8_seed(ctx)
        p_create.assert_called_once()          # re-ran, so i2 gets created
        self.assertEqual(ctx.state["injects_fingerprint"],
                         injects_config.injects_fingerprint([{"title": "i1"}, {"title": "i2"}]))

    def test_unchanged_injects_still_short_circuit(self):
        with tempfile.TemporaryDirectory() as d:
            injects = [{"title": "i1"}]
            ctx = _ctx(Path(d), state={"seeded": True, "engine_unpaused": True,
                                       "injects_created": True,
                                       "injects_fingerprint":
                                           injects_config.injects_fingerprint(injects)},
                       injects=injects)
            with dpatch("wait_for_http"), \
                    dpatch("engine_paused", return_value=False), \
                    dpatch("create_injects") as p_create, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                dl_seed.phase8_seed(ctx)
        p_create.assert_not_called()
        self.assertIn("already created", out.getvalue())

    def test_the_recorded_fingerprint_is_the_pre_resolve_one(self):
        definitions = [{"title": "i1", "open_offset_min": 0, "due_offset_min": 30}]
        expected = injects_config.injects_fingerprint(definitions)
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), state={"seeded": True, "engine_unpaused": True},
                       injects=[dict(definitions[0])])
            with dpatch("wait_for_http"), \
                    dpatch("engine_paused", return_value=False), \
                    dpatch("create_injects", return_value=([], [])), \
                    contextlib.redirect_stdout(io.StringIO()):
                dl_seed.phase8_seed(ctx)      # real resolve_inject_times runs
        self.assertEqual(ctx.state["injects_fingerprint"], expected)

    def test_fingerprint_tracks_titles_windows_and_attachments(self):
        base = [{"title": "i1", "open_offset_min": 0, "due_offset_min": 60,
                 "attachments": [{"name": "brief.txt"}]}]
        same = [{"title": "i1", "open_offset_min": 0, "due_offset_min": 60,
                 "attachments": [{"name": "brief.txt"}]}]
        new_title = [{"title": "i2", "open_offset_min": 0, "due_offset_min": 60,
                      "attachments": [{"name": "brief.txt"}]}]
        new_window = [{"title": "i1", "open_offset_min": 0, "due_offset_min": 90,
                       "attachments": [{"name": "brief.txt"}]}]
        new_file = [{"title": "i1", "open_offset_min": 0, "due_offset_min": 60,
                     "attachments": [{"name": "other.txt"}]}]
        self.assertEqual(injects_config.injects_fingerprint(base),
                         injects_config.injects_fingerprint(same))
        for changed in (new_title, new_window, new_file):
            self.assertNotEqual(injects_config.injects_fingerprint(base),
                                injects_config.injects_fingerprint(changed))
        self.assertEqual(injects_config.injects_fingerprint([]),
                         injects_config.injects_fingerprint(None))


class Phase2EngineTemplate(unittest.TestCase):
# The reuse-vs-rebuild decision, which is the whole point of the M4 hash gate.

    ENV = {"TEZ_PROXMOX_TEMPLATE_VM_ID": "9000", "TEZ_ENGINE_USERNAME": "ubuntu",
           "TEZ_SSH_PUBLIC_KEY": "ssh-rsa AAAA", "TEZ_PROXMOX_ENDPOINT": "https://pve:8006/",
           "TEZ_ENGINE_MGMT_GW": "10.0.0.1"}

    def _run(self, ctx, stored_node_hash):
        record = types.SimpleNamespace(destroyed=[], built=0, tf=[], retagged=[])
        # ExitStack, not one giant with: this harness sits at Python's 20-block
        # compile limit, and a 20th statically nested with-item is a SyntaxError.
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, self.ENV))
            for target, kwargs in [
                ("compfile_value", dict(return_value="")),
                ("engine_hash_inputs", dict(return_value={"a": 1})),
                ("hash_from_inputs", dict(return_value="HASH")),
                ("load_template_hashes",
                 dict(return_value={"engine": {"hash": "HASH", "inputs": {}}})),
                ("find_engine_template", dict(return_value=900)),
                ("template_stamp_current",
                 dict(side_effect=lambda node, vmid, expected: stored_node_hash == expected)),
                ("frozen_gate", dict(return_value=True)),
                ("save_template_hashes", {}),
                ("write_text_atomic", {}),
                ("forget_engine_host_key", {}),
                ("wait_for_ssh", {}),
                # The estate seams: the engine/bridge create, its coordinates, and the
                # guest-side netplan are all exercised by their own tests.
                ("configure_engine_network", {}),
                ("engine_context",
                 dict(return_value={"scoring_engine_ip": "10.0.0.99", "ssh_key_path": "/k",
                                    "vm_username": "ubuntu"})),
            ]:
                stack.enter_context(dpatch(target, **kwargs))
            stack.enter_context(dpatch("destroy_vm_if_exists",
                side_effect=lambda *a, **k: record.destroyed.append(a[1])))
            stack.enter_context(dpatch("destroy_engine_template",
                side_effect=lambda *a, **k: record.destroyed.append(a[1])))
            stack.enter_context(dpatch("retag_ownership",
                side_effect=lambda *a, **k: record.retagged.append(a[1])))
            stack.enter_context(dpatch("build_engine_template",
                side_effect=lambda *a, **k: (record.__setattr__("built", record.built + 1)
                                             or (901, {"built": True}))))
            stack.enter_context(dpatch("create_engine_and_bridges",
                side_effect=lambda c: record.__setattr__(
                    "engine_clone_id", c.state.get("engine_template_vmid"))))
            stack.enter_context(_nulltimed())
            out = io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(out))
            dl_engine.phase2_engine_template(ctx)
        return record, out.getvalue()

    def test_matching_hash_reuses_the_template_without_destroying_anything(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d))
            record, _out = self._run(ctx, stored_node_hash="HASH")
        self.assertEqual(record.destroyed, [])
        self.assertEqual(record.built, 0)
        # Reuse re-stamps ownership to this run (a template adopted from a
        # pre-run-id deploy carries no run tag).
        self.assertEqual(record.retagged, [900])
        self.assertEqual(ctx.state["engine_template_vmid"], 900)
        self.assertEqual(record.engine_clone_id, 900)
        self.assertEqual(ctx.state["deployed_endpoint"], "https://pve:8006")

    def test_drifted_hash_rebuilds_and_destroys_the_old_template(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d))
            record, out = self._run(ctx, stored_node_hash="DIFFERENT")
        self.assertIn("hash differs — rebuilding", out)
        self.assertEqual(record.destroyed, [1000, 1000])   # engine clone, then its template
        self.assertEqual(record.built, 1)
        self.assertEqual(ctx.state["engine_template_vmid"], 901)
        self.assertEqual(ctx.state["engine_build_info"], {"built": True})


if __name__ == "__main__":
    unittest.main()
