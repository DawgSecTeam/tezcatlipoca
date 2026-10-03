"""Per-phase unit tests for the extracted deploy phases (deploy_phases.py).

Only the decidable parts are tested — the branching, ordering and state writes that
used to be buried in deploy(). Every infrastructure call is patched: none of these
tests touches Proxmox, terraform, SSH or the network. They are the offline half of
"the phase boundary is a call boundary"; the byte-identity of the shell/config these
phases generate is covered by the modules that own that generation.
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
import config_ops  # noqa: E402
import deploy_phases  # noqa: E402
from nakon_ops import NakonResult  # noqa: E402


def _ctx(comp_dir, **overrides):
    """A DeployContext with harmless defaults; override what a test exercises."""
    fields = dict(
        comp_dir=comp_dir, comp_name=comp_dir.name,
        state_path=comp_dir / ".deploy_state.json", from_phase=1, resuming=False,
        assume_yes=True, name="probe", scenario="probe", box_username="ubuntu",
        credlist_usernames=["admin"], nakon_jobs=4, apt_cache=True, injects=[],
        packet_pw=None, state={}, teams={}, number_of_teams=0, admin_password="a",
        scoring_password="s", postgres_password="p", redis_password="r", box_password="b", box_creds={},
        domain_creds=None, inject_password=None, placement=None, node="pve",
        engine_vmid=1000, engine_mgmt_ip="10.0.0.1", boxes=[], boxes_by_name={},
        unbooted=set(), nakon_config_path=comp_dir / "nakon.json",
        golden_config_path=comp_dir / "golden.json",
        repair_config_path=comp_dir / "repair.json",
        final_config_path=comp_dir / "final.json",
        golden_inputs={}, golden_hashes={}, frozen_keep=set(), tf_dir=comp_dir,
        tfvars_path=comp_dir / "terraform.tfvars.json", tfvars={}, ssh_key_abs="/k",
        all_targets=[], managed_targets=[], linux_targets=[], windows_targets=[],
        tf_ctx={"ssh_key_path": "/k", "scoring_engine_ip": "10.0.0.99",
                "vm_username": "ubuntu"},
        ssh_key=Path("/k"), scoring_user="ubuntu", scoring_ip="10.0.0.99",
    )
    fields.update(overrides)
    return deploy.DeployContext(**fields)


def _nulltimed():
    return patch.object(deploy_phases, "timed",
                        side_effect=lambda *a, **k: contextlib.nullcontext())


class PhaseResumeGuards(unittest.TestCase):
    """from_phase past a phase must print its banner and touch nothing at all."""

    def test_phase3_skips_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), from_phase=4)
            with patch.object(deploy_phases, "prepare_engine_from_template") as p_prep, \
                    patch.object(deploy_phases, "push_event_conf") as p_push, \
                    patch.object(deploy_phases, "ensure_nat_forwarding") as p_nat, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                deploy_phases.phase3_prepare_engine(ctx)
        self.assertEqual(out.getvalue().strip(), "[3/7] Skipped (resume).")
        p_prep.assert_not_called()
        p_push.assert_not_called()
        p_nat.assert_not_called()

    def test_phase7_skips_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), from_phase=8)
            with patch.object(deploy_phases, "wait_for_http") as p_http, \
                    patch.object(deploy_phases, "seed_teams") as p_seed, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                deploy_phases.phase7_seed(ctx)
        self.assertEqual(out.getvalue().strip(), "[7/7] Skipped (resume).")
        p_http.assert_not_called()
        p_seed.assert_not_called()

    def test_phase1_skips_without_destroying(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), from_phase=2)
            with patch.object(deploy_phases, "destroy_node_waves") as p_waves, \
                    patch.object(deploy_phases, "destroy_bridge_if_exists") as p_bridge, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                deploy_phases.phase1_cleanup(ctx)
        self.assertEqual(out.getvalue().strip(),
                         "[1/7] Skipped (resume) — leaving existing VMs/bridges in place.")
        p_waves.assert_not_called()
        p_bridge.assert_not_called()


class Phase1Cleanup(unittest.TestCase):
    def _run(self, ctx, bridge_in_use=False):
        calls = types.SimpleNamespace(waves=[], bridges=[], markers=[], sleeps=[])
        with patch.object(deploy_phases, "destroy_node_waves",
                          side_effect=lambda *a, **k: calls.waves.append((a, k))), \
                patch.object(deploy_phases, "destroy_bridge_if_exists",
                             side_effect=lambda *a: calls.bridges.append(a)), \
                patch.object(deploy_phases, "_bridge_in_use", return_value=bridge_in_use), \
                patch.object(deploy_phases, "timed",
                             side_effect=lambda *a, **k: contextlib.nullcontext()), \
                patch.object(deploy_phases, "time") as p_time, \
                patch.object(deploy, "reset_domain_markers",
                             side_effect=lambda c: calls.markers.append(c)), \
                contextlib.redirect_stdout(io.StringIO()):
            p_time.sleep.side_effect = lambda s: calls.sleeps.append(s)
            deploy_phases.phase1_cleanup(ctx)
        return calls

    def test_fresh_run_merges_legacy_clones_and_destroys_wave_1(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            (comp_dir / "cloned_vms.json").write_text(json.dumps({"web01": 101}))
            (comp_dir / ".postclone-swept").write_text("stale")
            target = {"vmid": 201, "vm_name": "web01-team101", "node": "pve"}
            ctx = _ctx(comp_dir, teams={"team1": {"identifier": "101", "password": "x"}},
                       all_targets=[target])
            calls = self._run(ctx)
        # legacy API clones are folded into ctx.legacy_clones for wave 1
        self.assertEqual(ctx.legacy_clones, {101: "web01"})
        self.assertEqual(len(calls.waves), 1)
        (args, kwargs), = calls.waves
        self.assertEqual(args[1:], ("pve", [target], 0))          # node, that node's targets, slot 0
        self.assertEqual(calls.bridges, [("pve", "vmbr101")])
        self.assertEqual(calls.sleeps, [5])                        # settle before the checkpoint
        self.assertEqual(calls.markers, [comp_dir])
        self.assertFalse((comp_dir / ".postclone-swept").exists())

    def test_unparseable_cloned_vms_warns_and_continues(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            (comp_dir / "cloned_vms.json").write_text("{not json")
            ctx = _ctx(comp_dir, teams={}, all_targets=[])
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self._run_with_stdout(ctx, out)
        self.assertIn("could not parse cloned_vms.json", out.getvalue())

    def _run_with_stdout(self, ctx, out):
        with patch.object(deploy_phases, "destroy_node_waves"), \
                patch.object(deploy_phases, "destroy_bridge_if_exists"), \
                patch.object(deploy_phases, "timed",
                             side_effect=lambda *a, **k: contextlib.nullcontext()), \
                patch.object(deploy_phases, "time"), \
                patch.object(deploy, "reset_domain_markers"), \
                contextlib.redirect_stdout(out):
            deploy_phases.phase1_cleanup(ctx)

    def test_placement_splits_bridges_and_jumps_by_host(self):
        placement = {
            "engine_node": "pve", "team_nodes": {"team1": "sat1", "team2": "pve"},
            "satellites": [{"name": "sat1", "slot": 1, "jump_vmid": 9001,
                            "anchor_identifier": "120"}],
            "nodes": {"sat1": {}, "pve": {}},
            "team_slots": {"team1": 1, "team2": 0},
        }
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            ctx = _ctx(comp_dir, comp_name="probe", placement=placement,
                       teams={"team1": {"identifier": "101", "password": "x"},
                              "team2": {"identifier": "102", "password": "y"}},
                       all_targets=[])
            with patch.object(deploy_phases, "record_of",
                              return_value=types.SimpleNamespace(node="sat-node")):
                calls = self._run(ctx)
        # one wave pair per hosting node: the engine node's slot 0, then the satellite's
        slots = [a[3] for a, _kw in calls.waves]
        self.assertEqual(slots, [0, 1])
        sat_args, sat_kwargs = calls.waves[1]
        self.assertEqual(sat_args[1], "sat-node")
        self.assertEqual(sat_kwargs["extra_destroy"], {9001: "jump-probe-1"})
        # satellite team's bridge dies on the satellite host; engine-node team's on pve
        self.assertEqual(calls.bridges, [("sat-node", "vmbr101"), ("pve", "vmbr102")])

    def test_bridge_with_attached_vms_is_not_destroyed(self):
        # 2026-10-02: bridge names (vmbr<identifier>) collide across same-comp deploys
        # sharing team identifiers — a bridge with ANY attached VM survives phase 1.
        placement = {
            "engine_node": "pve", "team_nodes": {"team1": "pve"},
            "satellites": [], "nodes": {"pve": {}}, "team_slots": {"team1": 0},
        }
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            ctx = _ctx(comp_dir, comp_name="probe", placement=placement,
                       teams={"team1": {"identifier": "101", "password": "x"}},
                       all_targets=[])
            calls = self._run(ctx, bridge_in_use=True)
        self.assertEqual(calls.bridges, [])


class Phase3Order(unittest.TestCase):
    def test_engine_prep_precedes_event_conf_precedes_nat(self):
        with tempfile.TemporaryDirectory() as d:
            order = []
            ctx = _ctx(Path(d))
            with patch.object(deploy_phases, "prepare_engine_from_template",
                              side_effect=lambda *a: order.append("template")), \
                    patch.object(deploy_phases, "push_event_conf",
                                 side_effect=lambda *a, **k: order.append("event_conf")), \
                    patch.object(deploy_phases, "ensure_nat_forwarding",
                                 side_effect=lambda *a: order.append("nat")), \
                    contextlib.redirect_stdout(io.StringIO()):
                deploy_phases.phase3_prepare_engine(ctx)
        self.assertEqual(order, ["template", "event_conf", "nat"])


class Phase5RepairSweep(unittest.TestCase):
    def test_resume_marker_skips_the_whole_sweep(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            marker = comp_dir / ".postclone-swept"
            marker.write_text("done")
            ctx = _ctx(comp_dir)
            with patch.object(deploy_phases, "run_nakon") as p_nakon, \
                    patch.object(deploy_phases, "fix_services_on_boxes") as p_fix, \
                    patch.object(deploy_phases, "ensure_nat_forwarding") as p_nat, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                deploy_phases.phase5_repair_sweep(ctx)
            self.assertIn("[5/7] Resume marker present", out.getvalue())
            p_nakon.assert_not_called()
            p_fix.assert_not_called()
            p_nat.assert_not_called()
            self.assertEqual(marker.read_text(), "done")

    def test_no_repair_machines_still_runs_fix_services_and_writes_marker(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            ctx = _ctx(comp_dir)
            ctx.repair_config_path.write_text(json.dumps({"machines": []}))
            with patch.object(deploy_phases, "run_nakon") as p_nakon, \
                    patch.object(deploy_phases, "fix_services_on_boxes") as p_fix, \
                    patch.object(deploy_phases, "ensure_nat_forwarding"), \
                    patch.object(deploy_phases, "compfile_flag", return_value=0), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                deploy_phases.phase5_repair_sweep(ctx)
            self.assertIn("No repair-stage configurations in this lineup", out.getvalue())
            p_nakon.assert_not_called()
            p_fix.assert_called_once()
            self.assertTrue((comp_dir / ".postclone-swept").exists())

    def test_a_lineup_with_no_postclone_configs_records_a_clean_verdict(self):
        """Live-found 2026-10-02: a lineup whose configs are ALL golden-stage
        (same-type-2box) never reached record_stage_coverage, so
        `plant_coverage_failed` was never created and verify's coverage gate failed
        closed on a completely clean run — "coverage was never recorded (pre-tally
        deploy?)". "Nothing to plant post-clone" is a clean result, not an absent one."""
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            ctx = _ctx(comp_dir)
            ctx.repair_config_path.write_text(json.dumps({"machines": []}))
            with patch.object(deploy_phases, "run_nakon"), \
                    patch.object(deploy_phases, "fix_services_on_boxes"), \
                    patch.object(deploy_phases, "ensure_nat_forwarding"), \
                    patch.object(deploy_phases, "compfile_flag", return_value=0), \
                    contextlib.redirect_stdout(io.StringIO()):
                deploy_phases.phase5_repair_sweep(ctx)
        self.assertIn("plant_coverage_failed", ctx.state)
        self.assertEqual(ctx.state["plant_coverage_failed"], {})
        self.assertEqual(ctx.state["nakon_failed_steps"], [])

    def test_repair_pass_records_the_stage_tally_and_persists_coverage(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            machines = [{"name": "web01-team101", "configurations": []}]
            ctx = _ctx(comp_dir)
            ctx.repair_config_path.write_text(json.dumps({"machines": machines}))
            result = NakonResult(["sshd-hardening"],
                                 [{"name": "web01-team101", "steps": []}])
            with patch.object(deploy_phases, "run_nakon", return_value=result) as p_nakon, \
                    patch.object(deploy_phases, "build_nakon_bundle", return_value="bundle"), \
                    patch.object(deploy_phases, "fix_services_on_boxes"), \
                    patch.object(deploy_phases, "ensure_nat_forwarding"), \
                    patch.object(deploy_phases, "compfile_flag", return_value=0), \
                    patch.object(deploy, "record_stage_coverage") as p_coverage, \
                    contextlib.redirect_stdout(io.StringIO()):
                deploy_phases.phase5_repair_sweep(ctx)
            self.assertEqual(ctx.state["nakon_failed_steps"], ["repair: sshd-hardening"])
            _args = p_nakon.call_args
            self.assertIs(_args.kwargs["strict"], False)
            self.assertEqual(_args.kwargs["jobs"], 4)
            # the coverage record always saves through the context's own writer
            saved = p_coverage.call_args.args[3]
            self.assertIs(saved.__self__, ctx)
            self.assertIs(saved.__func__, deploy.DeployContext.save_state)


class Phase4GoldenCoverage(unittest.TestCase):
    """Phase 4 records the golden plant's coverage verdict like a post-clone stage's:
    the closure threaded into build_golden_set writes plant_coverage_failed (and the
    stage-prefixed tally on tolerated failures) and persists through ctx.save_state.
    The integration halves join here with the REAL check_plant_coverage — a golden-only
    lineup (same-type-2box shape) must come out PASS on a clean plant and FAIL, with
    the (golden-stage) annotation, on an alpine-tolerated one."""

    def _run_phase4(self, comp_dir):
        """Run phase4_golden_set with build_golden_set faked; returns (ctx, coverage)."""
        captured = []
        ctx = _ctx(comp_dir, golden_hashes={}, golden_inputs={})
        with patch.object(deploy_phases, "load_template_hashes", return_value={}), \
                patch.object(deploy, "golden_rebuild_gate"), \
                patch.object(deploy_phases, "build_golden_set",
                             side_effect=lambda *a, **k:
                                 (captured.append(k["coverage"]) or {"web01": 1250})), \
                patch.object(deploy_phases, "_template_vmid_map", return_value={}), \
                patch.object(deploy_phases, "write_text_atomic"), \
                patch.object(deploy_phases, "terraform_plugin_cache_dir",
                             return_value=comp_dir), \
                patch.object(deploy_phases, "terraform_dir", return_value=comp_dir), \
                patch.object(deploy_phases, "run_terraform"), \
                patch.object(deploy_phases, "run_concurrent", return_value=[]), \
                patch.object(deploy_phases, "wait_for_boxes_ssh"), \
                patch.object(deploy_phases, "wait_for_cloud_init"), \
                patch.object(deploy_phases, "setup_ubuntu_auth"), \
                patch.object(deploy_phases, "fix_dns_on_boxes"), \
                patch.object(deploy_phases, "prep_apt_on_boxes"), \
                patch.object(deploy_phases, "snap_base"), \
                _nulltimed(), \
                contextlib.redirect_stdout(io.StringIO()):
            deploy_phases.phase4_golden_set(ctx)
        return ctx, captured[0]

    @staticmethod
    def _gate(comp_dir, machines):
        """The real verify gate against a real comp dir (importlib: dashed filename).
        Returns (gate, stdout); status compared by NAME — this helper loads its own
        module copy, so the gate's Status enum is not the caller's object."""
        (comp_dir / "nakon-config.json").write_text(
            json.dumps({"machines": machines}))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "verify_phase4_join", _REPO / "verify-competition.py")
        verify = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verify)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gate = verify.check_plant_coverage(comp_dir)
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
            on_disk = json.loads((comp_dir / ".deploy_state.json").read_text())
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
        """result=None (reuse / checkpoint / pristine / cold): the record must come
        into existence if absent, but a previously recorded failure survives — the
        disk is proven by hash, not by a fresh plant."""
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
        with patch.object(deploy_phases, "wait_for_http",
                          side_effect=lambda *a, **k: order.append("http")), \
                patch.object(deploy_phases, "seed_teams",
                             side_effect=lambda *a: order.append("seed")), \
                patch.object(deploy_phases, "engine_paused", return_value=True), \
                patch.object(deploy_phases, "unpause_engine",
                             side_effect=lambda *a: order.append("unpause")), \
                patch.object(deploy_phases, "resolve_inject_times",
                             side_effect=lambda *a: order.append("resolve")), \
                patch.object(deploy_phases, "create_injects",
                             side_effect=lambda *a: order.append("injects")
                             or ([], [])), \
                contextlib.redirect_stdout(io.StringIO()):
            deploy_phases.phase7_seed(ctx)
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
            with patch.object(deploy_phases, "wait_for_http"), \
                    patch.object(deploy_phases, "seed_teams"), \
                    patch.object(deploy_phases, "engine_paused", return_value=False), \
                    patch.object(deploy_phases, "unpause_engine") as p_unpause, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                deploy_phases.phase7_seed(ctx)
        p_unpause.assert_not_called()
        self.assertIn("Engine reports itself unpaused", out.getvalue())
        self.assertTrue(ctx.state["engine_unpaused"])

    def test_failed_injects_are_not_marked_created(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), state={"seeded": True, "engine_unpaused": True},
                       injects=[{"title": "i1"}])
            with patch.object(deploy_phases, "wait_for_http"), \
                    patch.object(deploy_phases, "engine_paused", return_value=False), \
                    patch.object(deploy_phases, "resolve_inject_times"), \
                    patch.object(deploy_phases, "create_injects",
                                 return_value=([], ["Bad inject"])), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                deploy_phases.phase7_seed(ctx)
        self.assertNotIn("injects_created", ctx.state)
        self.assertIn("1 inject(s) failed to create: Bad inject", out.getvalue())

    def test_an_inject_added_after_the_first_run_is_not_skipped(self):
        """The bug: `injects_created` was a bare boolean, so a new inject was never
        posted on a resume — create_injects dedups on titles, but nothing re-ran it."""
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), state={"seeded": True, "engine_unpaused": True},
                       injects=[{"title": "i1"}, {"title": "i2"}])
            stale = config_ops.injects_fingerprint([{"title": "i1"}])
            ctx.state["injects_created"] = True
            ctx.state["injects_fingerprint"] = stale
            with patch.object(deploy_phases, "wait_for_http"), \
                    patch.object(deploy_phases, "engine_paused", return_value=False), \
                    patch.object(deploy_phases, "resolve_inject_times"), \
                    patch.object(deploy_phases, "create_injects",
                                 return_value=([], [])) as p_create, \
                    contextlib.redirect_stdout(io.StringIO()):
                deploy_phases.phase7_seed(ctx)
        p_create.assert_called_once()          # re-ran, so i2 gets created
        self.assertEqual(ctx.state["injects_fingerprint"],
                         config_ops.injects_fingerprint([{"title": "i1"}, {"title": "i2"}]))

    def test_unchanged_injects_still_short_circuit(self):
        with tempfile.TemporaryDirectory() as d:
            injects = [{"title": "i1"}]
            ctx = _ctx(Path(d), state={"seeded": True, "engine_unpaused": True,
                                       "injects_created": True,
                                       "injects_fingerprint":
                                           config_ops.injects_fingerprint(injects)},
                       injects=injects)
            with patch.object(deploy_phases, "wait_for_http"), \
                    patch.object(deploy_phases, "engine_paused", return_value=False), \
                    patch.object(deploy_phases, "create_injects") as p_create, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                deploy_phases.phase7_seed(ctx)
        p_create.assert_not_called()
        self.assertIn("already created", out.getvalue())

    def test_fingerprint_must_be_taken_before_offsets_are_popped(self):
        """resolve_inject_times pops the offset keys, so a fingerprint taken after it
        loses a non-default window (30 -> the 60 default) and would re-create the set
        spuriously on the next run. Default offsets happen to survive, which is exactly
        why this must be pinned on a non-default one."""
        loaded = [{"title": "i1", "open_offset_min": 0, "due_offset_min": 30}]
        before = config_ops.injects_fingerprint(loaded)
        config_ops.resolve_inject_times(loaded)
        after = config_ops.injects_fingerprint(loaded)
        self.assertNotEqual(before, after)
        self.assertEqual(before, config_ops.injects_fingerprint(
            [{"title": "i1", "open_offset_min": 0, "due_offset_min": 30}]))

    def test_the_recorded_fingerprint_is_the_pre_resolve_one(self):
        definitions = [{"title": "i1", "open_offset_min": 0, "due_offset_min": 30}]
        expected = config_ops.injects_fingerprint(definitions)
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), state={"seeded": True, "engine_unpaused": True},
                       injects=[dict(definitions[0])])
            with patch.object(deploy_phases, "wait_for_http"), \
                    patch.object(deploy_phases, "engine_paused", return_value=False), \
                    patch.object(deploy_phases, "create_injects", return_value=([], [])), \
                    contextlib.redirect_stdout(io.StringIO()):
                deploy_phases.phase7_seed(ctx)      # real resolve_inject_times runs
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
        self.assertEqual(config_ops.injects_fingerprint(base),
                         config_ops.injects_fingerprint(same))
        for changed in (new_title, new_window, new_file):
            self.assertNotEqual(config_ops.injects_fingerprint(base),
                                config_ops.injects_fingerprint(changed))
        self.assertEqual(config_ops.injects_fingerprint([]),
                         config_ops.injects_fingerprint(None))


class Phase2EngineTemplate(unittest.TestCase):
    """The reuse-vs-rebuild decision, which is the whole point of the M4 hash gate."""

    ENV = {"TF_VAR_template_vm_id": "9000", "TF_VAR_vm_username": "ubuntu",
           "TF_VAR_ssh_public_key": "ssh-rsa AAAA", "TF_VAR_proxmox_endpoint": "https://pve:8006/",
           "TF_VAR_engine_mgmt_gw": "10.0.0.1"}

    def setUp(self):
        if not Path("terraform/main.tf").exists():
            self.skipTest("needs the repo as CWD (reads terraform/main.tf)")

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
                ("stored_template_hash", dict(return_value=stored_node_hash)),
                ("frozen_gate", dict(return_value=True)),
                ("save_template_hashes", {}),
                ("write_text_atomic", {}),
                ("terraform_dir", dict(return_value=ctx.comp_dir)),
                ("terraform_plugin_cache_dir", dict(return_value=ctx.comp_dir)),
                ("forget_engine_host_key", {}),
                ("wait_for_ssh", {}),
            ]:
                stack.enter_context(patch.object(deploy_phases, target, **kwargs))
            stack.enter_context(patch.object(
                deploy_phases, "destroy_vm_if_exists",
                side_effect=lambda *a, **k: record.destroyed.append(a[1])))
            stack.enter_context(patch.object(
                deploy_phases, "destroy_engine_template",
                side_effect=lambda *a, **k: record.destroyed.append(a[1])))
            stack.enter_context(patch.object(
                deploy_phases, "retag_ownership",
                side_effect=lambda *a, **k: record.retagged.append(a[1])))
            stack.enter_context(patch.object(
                deploy_phases, "build_engine_template",
                side_effect=lambda *a, **k: (record.__setattr__("built", record.built + 1)
                                             or (901, {"built": True}))))
            stack.enter_context(patch.object(
                deploy_phases, "run_terraform",
                side_effect=lambda argv, **k: record.tf.append(argv)))
            stack.enter_context(patch.object(
                deploy_phases, "read_terraform_ctx",
                side_effect=lambda *a, **k: {"scoring_engine_ip": "10.0.0.99",
                                             "ssh_key_path": "/k", "vm_username": "ubuntu"}))
            stack.enter_context(_nulltimed())
            out = io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(out))
            deploy_phases.phase2_engine_template(ctx)
        return record, out.getvalue()

    def test_matching_hash_reuses_the_template_without_destroying_anything(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), tfvars={})
            record, _out = self._run(ctx, stored_node_hash="HASH")
        self.assertEqual(record.destroyed, [])
        self.assertEqual(record.built, 0)
        # Reuse re-stamps ownership to this run (a template adopted from a
        # pre-run-id deploy carries no run tag).
        self.assertEqual(record.retagged, [900])
        self.assertEqual(ctx.state["engine_template_vmid"], 900)
        self.assertEqual(ctx.tfvars["engine_clone_id"], 900)
        self.assertEqual(ctx.state["deployed_endpoint"], "https://pve:8006")
        self.assertEqual([a[0] for a in record.tf], ["init", "apply"])

    def test_drifted_hash_rebuilds_and_destroys_the_old_template(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), tfvars={})
            record, out = self._run(ctx, stored_node_hash="DIFFERENT")
        self.assertIn("hash differs — rebuilding", out)
        self.assertEqual(record.destroyed, [1000, 1000])   # engine clone, then its template
        self.assertEqual(record.built, 1)
        self.assertEqual(ctx.state["engine_template_vmid"], 901)
        self.assertEqual(ctx.state["engine_build_info"], {"built": True})


class PrepareResumeGuards(unittest.TestCase):
    """prepare()'s resume wiring: the pipeline-version gate and the phase guard."""

    def _comp_dir(self, tmp, state):
        comp_dir = Path(tmp)
        (comp_dir / "Compfile").write_text("name probe\nscenario probe\ndifficulty 1\n")
        (comp_dir / "boxes.json").write_text(json.dumps(
            [{"name": "web01", "template": "ubuntu-fix", "cpu": 1,
              "memory_mb": 1024, "last_octet": 2}]))
        (comp_dir / ".deploy_state.json").write_text(json.dumps(state))
        return comp_dir

    def test_resume_past_the_last_completed_phase_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = self._comp_dir(d, {"pipeline_version": 2, "last_phase": 3,
                                          "scoring_vm_id": 1000, "teams": {}})
            with self.assertRaises(SystemExit) as ctx:
                deploy.prepare(comp_dir, from_phase=6, num_teams=1, assume_yes=True)
        msg = str(ctx.exception)
        self.assertIn("--from-phase 6", msg)
        self.assertIn("records phase 3", msg)

    def test_resuming_the_phase_after_the_last_completed_one_is_allowed(self):
        # The guard passes; prove it by failing later, in placement.
        with tempfile.TemporaryDirectory() as d:
            comp_dir = self._comp_dir(d, {"pipeline_version": 2, "last_phase": 2,
                                          "scoring_vm_id": 1000, "teams": {},
                                          "admin_password": "a", "box_password": "b"})
            with patch.object(deploy, "load_injects", return_value=[]), \
                    patch.object(deploy, "load_packet_passwords", return_value=None), \
                    patch.object(deploy, "resolve_placement",
                                 side_effect=RuntimeError("past the guard")):
                with self.assertRaises(RuntimeError) as ctx:
                    deploy.prepare(comp_dir, from_phase=3, num_teams=1, assume_yes=True)
        self.assertEqual(str(ctx.exception), "past the guard")

    def test_force_from_phase_bypasses_the_guard(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = self._comp_dir(d, {"pipeline_version": 2, "last_phase": 2,
                                          "scoring_vm_id": 1000, "teams": {},
                                          "admin_password": "a", "box_password": "b"})
            with patch.object(deploy, "load_injects", return_value=[]), \
                    patch.object(deploy, "load_packet_passwords", return_value=None), \
                    patch.object(deploy, "resolve_placement",
                                 side_effect=RuntimeError("past the guard")):
                with self.assertRaises(RuntimeError) as ctx:
                    deploy.prepare(comp_dir, from_phase=6, num_teams=1, assume_yes=True,
                                   force_from_phase=True)
        self.assertEqual(str(ctx.exception), "past the guard")

    def test_a_pipeline_v1_state_file_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = self._comp_dir(d, {"pipeline_version": 1, "last_phase": 3,
                                          "scoring_vm_id": 1000, "teams": {}})
            with self.assertRaises(SystemExit) as ctx:
                deploy.prepare(comp_dir, from_phase=2, num_teams=1, assume_yes=True)
        self.assertIn("pipeline v1", str(ctx.exception))

    def test_resuming_without_a_state_file_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            (comp_dir / "Compfile").write_text("name probe\nscenario probe\ndifficulty 1\n")
            (comp_dir / "boxes.json").write_text(json.dumps(
                [{"name": "web01", "template": "ubuntu-fix", "cpu": 1,
                  "memory_mb": 1024, "last_octet": 2}]))
            with self.assertRaises(SystemExit) as ctx:
                deploy.prepare(comp_dir, from_phase=2, num_teams=1, assume_yes=True)
        self.assertIn("doesn't exist", str(ctx.exception))


class PrepareTfvarsAssembly(unittest.TestCase):
    """prepare()'s tfvars/tfvars.json assembly and its cancel early return.

    The operators' confirmation prompt is the one early return deploy() had, and
    terraform.tfvars.json is what terraform actually reads (it outranks TF_VAR_*),
    so both are worth pinning offline."""

    ENV = {
        "TF_VAR_ssh_private_key_path": "/tmp/key", "TF_VAR_proxmox_node": "pve",
        "TF_VAR_vm_username": "ubuntu", "TF_VAR_ssh_public_key": "ssh-rsa AAAA",
        "TF_VAR_proxmox_endpoint": "https://pve:8006/",
        # explicit "" is the documented DHCP switch; the static default is skipped
        "TF_VAR_engine_mgmt_ip": "",
    }

    def _prepare(self, comp_dir, captured):
        teams = {"team1": {"identifier": "101", "password": "pw"}}
        with patch.dict(os.environ, self.ENV), \
                patch.object(deploy, "collect_teams", return_value=teams), \
                patch.object(deploy, "write_state"), \
                patch.object(deploy, "update_env") as p_update_env, \
                patch.object(deploy, "resolve_placement", return_value=(None, None)), \
                patch.object(deploy, "acquire_engine_lock"), \
                patch.object(deploy, "generate_nakon_config",
                             return_value=comp_dir / "nakon.json"), \
                patch.object(deploy, "unbooted_golden_boxes", return_value=set()), \
                patch.object(deploy, "generate_stage_configs",
                             return_value=(comp_dir / "g.json", comp_dir / "r.json",
                                           comp_dir / "f.json", None)), \
                patch.object(deploy, "build_nakon_bundle", return_value=object()), \
                patch.object(deploy, "_template_vmid_map", return_value={}), \
                patch.object(deploy, "golden_hash_entries", return_value=({}, {})), \
                patch.object(deploy, "frozen_state", return_value=None), \
                patch.object(deploy, "write_text_atomic",
                             side_effect=lambda p, t: captured.__setitem__(str(p), t)), \
                patch.object(deploy, "ensure_terraform_workdir", return_value=comp_dir), \
                patch.object(deploy, "preflight_gates") as p_preflight, \
                patch.object(deploy, "confirm_deploy", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = deploy.prepare(comp_dir, num_teams=1, assume_yes=False)
        return result, p_update_env, p_preflight, out.getvalue()

    def _comp_dir(self, tmp):
        comp_dir = Path(tmp)
        (comp_dir / "Compfile").write_text("name probe\nscenario probe\ndifficulty 3\n")
        (comp_dir / "boxes.json").write_text(json.dumps(
            [{"name": "web01", "template": "ubuntu-fix", "cpu": 1,
              "memory_mb": 2048, "disk_gb": 20, "last_octet": 2}]))
        # prepare() reads the golden stage config to map golden machines by box
        (comp_dir / "g.json").write_text(json.dumps({"machines": []}))
        return comp_dir

    def test_declining_the_prompt_returns_none_after_the_tfvars_write(self):
        captured = {}
        with tempfile.TemporaryDirectory() as d:
            comp_dir = self._comp_dir(d)
            result, _env, p_preflight, out = self._prepare(comp_dir, captured)
        self.assertIsNone(result)
        self.assertIn("Deployment cancelled.", out)
        tfvars = json.loads(captured[str(Path(comp_dir) / "terraform.tfvars.json")])
        self.assertEqual(tfvars["event_name"], "probe")
        self.assertEqual(tfvars["scoring_vm_id"], 1000)
        self.assertEqual(tfvars["box_username"], "ubuntu")
        self.assertEqual(tfvars["ssh_private_key_path"], "/tmp/key")
        self.assertEqual(tfvars["teams"], {"team1": {"identifier": "101", "password": "pw"}})
        # M3.3 two-apply: apply #1 must build no team boxes and carry no goldens
        self.assertIs(tfvars["build_team_boxes"], False)
        self.assertEqual(tfvars["golden_template_ids"], [])
        self.assertEqual(tfvars["engine_clone_id"], 0)
        self.assertNotIn("satellites", tfvars)          # single-node: no satellite keys
        # preflight ran before the prompt (check_free on a fresh, non-resuming run)
        self.assertIs(p_preflight.call_args.kwargs["check_free"], True)

    def test_scoring_vmid_flag_flows_into_env_and_tfvars(self):
        captured = {}
        with tempfile.TemporaryDirectory() as d:
            comp_dir = self._comp_dir(d)
            with patch.dict(os.environ, self.ENV), \
                    patch.object(deploy, "collect_teams",
                                 return_value={"team1": {"identifier": "101", "password": "pw"}}), \
                    patch.object(deploy, "write_state"), \
                    patch.object(deploy, "update_env") as p_update_env, \
                    patch.object(deploy, "resolve_placement", return_value=(None, None)), \
                    patch.object(deploy, "acquire_engine_lock"), \
                    patch.object(deploy, "generate_nakon_config",
                                 return_value=comp_dir / "nakon.json"), \
                    patch.object(deploy, "unbooted_golden_boxes", return_value=set()), \
                    patch.object(deploy, "generate_stage_configs",
                                 return_value=(comp_dir / "g.json", comp_dir / "r.json",
                                               comp_dir / "f.json", None)), \
                    patch.object(deploy, "build_nakon_bundle", return_value=object()), \
                    patch.object(deploy, "_template_vmid_map", return_value={}), \
                    patch.object(deploy, "golden_hash_entries", return_value=({}, {})), \
                    patch.object(deploy, "frozen_state", return_value=None), \
                    patch.object(deploy, "write_text_atomic",
                                 side_effect=lambda p, t: captured.__setitem__(str(p), t)), \
                    patch.object(deploy, "ensure_terraform_workdir", return_value=comp_dir), \
                    patch.object(deploy, "preflight_gates"), \
                    patch.object(deploy, "confirm_deploy", return_value=False), \
                    contextlib.redirect_stdout(io.StringIO()):
                deploy.prepare(comp_dir, num_teams=1, assume_yes=False, scoring_vmid=1234)
        tfvars = json.loads(captured[str(Path(comp_dir) / "terraform.tfvars.json")])
        self.assertEqual(tfvars["scoring_vm_id"], 1234)
        self.assertEqual(p_update_env.call_args.args[0]["TF_VAR_scoring_vm_id"], "1234")


if __name__ == "__main__":
    unittest.main()
