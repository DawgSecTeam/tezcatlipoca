"""Offline unit tests for prepare()'s extracted steps (each step fills the DeployContext it is given).

prepare() is a short sequencer over _load/_resolve/_apply/_build/_run/_enumerate helpers,
all filling one DeployContext. These tests are the call-boundary half of that split: the
decidable logic of each step (the resume guards, the golden gate's position, the stale-state
guard, the engine mgmt IP/gw defaults, the target split) and — most importantly — the ORDER
the sequencer must keep, because several of those orderings are load-bearing and commented.

Every infrastructure call is patched: none of this touches Proxmox, SSH or the network.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
import deploy  # noqa: E402
from _deploy_patch import dpatch  # noqa: E402
from infra import gates as dl_gates  # noqa: E402
from deploy_lib import configs as dl_configs  # noqa: E402
from deploy_lib import inputs as dl_inputs  # noqa: E402
from deploy_lib import placement as dl_placement  # noqa: E402
from deploy_lib import targets as dl_targets  # noqa: E402
from estate import config
from estate import state as estate_state
from deploy_lib import estate_inputs as dl_estate_inputs  # noqa: E402
from deploy_lib.context import DeployContext  # noqa: E402
from common.constants import (DEFAULT_ENGINE_MGMT_GW, DEFAULT_ENGINE_MGMT_IP,  # noqa: E402
                       SCORING_ENGINE_VMID)


def _ctx(**overrides):
    fields = dict(name="probe", scenario="probe", difficulty=3, box_username="ubuntu",
                  credlist_usernames=["admin"], nakon_jobs=4, apt_cache=True,
                  comp_name="probe", boxes=[{"name": "web01", "template": "ubuntu-fix"}],
                  state={"last_phase": 0}, teams={"team1": {"identifier": "101", "password": "pw"}},
                  number_of_teams=1, admin_password="a", postgres_password="p",
                  redis_password="r", box_password="b", box_creds={}, domain_creds=None,
                  inject_password=None, engine_vmid=1000)
    fields.update(overrides)
    return DeployContext(**fields)


def _prior(comp_dir, **overrides):
    fields = dict(state_path=estate_state.state_path(comp_dir), previous_state={},
                  resuming=False)
    fields.update(overrides)
    return DeployContext(**fields)


class EngineVmidResolution(unittest.TestCase):
# The first step: the vmid everything else (the lock, phase-1 destroy) keys on.

    def test_fresh_run_takes_the_scoring_vmid_flag(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = DeployContext()
            dl_inputs.resolve_engine_vmid(ctx, Path(d), 1, 1234)
        self.assertEqual(ctx.engine_vmid, 1234)


    def test_fresh_run_without_a_flag_takes_the_env_vmid(self):
        # TEZ_SCORING_VMID ships in every env variant; caught live it was
        # silently ignored (the default 1000 was taken) until a second range collided.
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"TEZ_SCORING_VMID": "777"}):
                ctx = DeployContext()
                dl_inputs.resolve_engine_vmid(ctx, Path(d), 1, None)
        self.assertEqual(ctx.engine_vmid, 777)

    def test_a_non_numeric_env_vmid_refuses_by_name(self):
        # Silently taking the default is what made a wrong scoring vmid invisible; a bad
        # value must name the key instead.
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"TEZ_SCORING_VMID": "not-a-number"}):
                ctx = DeployContext()
                with self.assertRaises(config.ConfigError) as raised:
                    dl_inputs.resolve_engine_vmid(ctx, Path(d), 1, None)
        self.assertIn("knobs.scoring_vmid", str(raised.exception))
        self.assertIn("must be a number", str(raised.exception))

    def test_resume_reads_the_vmid_from_state_not_the_flag(self):
        # On resume the document is authoritative: the engine that exists at that vmid is
        # the one the lock and phase-1 destroy must address, whatever the flag says.
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            estate_state.write_flat(comp_dir, {"scoring_vm_id": 4321})
            ctx = DeployContext()
            dl_inputs.resolve_engine_vmid(ctx, comp_dir, 3, 9999)
        self.assertEqual(ctx.engine_vmid, 4321)


def _load_and_gate(ctx, comp_dir, from_phase, force):
# What prepare() does with the prior state: load it, then run the resume gates.
    dl_inputs.load_prior_deploy_state(ctx, comp_dir, from_phase)
    dl_gates.check_resume_gates(comp_dir, ctx.state_path, ctx.previous_state,
                                from_phase, force=force)


class PriorStateGuards(unittest.TestCase):
# The state read + the two refusals, plus the guard_resume_from_phase wiring.

    def _comp_dir(self, tmp, state=None):
        comp_dir = Path(tmp)
        if state is not None:
            estate_state.write_flat(comp_dir, state)
        return comp_dir

    def test_resume_without_state_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _prior(Path(d))
            with self.assertRaises(SystemExit) as raised:
                _load_and_gate(ctx, Path(d), 2, False)
        self.assertIn("doesn't exist", str(raised.exception))


    def test_pipeline_v2_state_is_refused(self):
        # A state from another pipeline version numbers its phases differently.
        with tempfile.TemporaryDirectory() as d:
            comp_dir = self._comp_dir(d, {"pipeline_version": 2, "last_phase": 3})
            ctx = _prior(comp_dir)
            with self.assertRaises(SystemExit) as raised:
                _load_and_gate(ctx, comp_dir, 2, False)
        self.assertIn("pipeline v2", str(raised.exception))
        self.assertIn("different things", str(raised.exception))




class PlacementAndLock(unittest.TestCase):
# Placement precedes the endpoint-keyed lock, and the lock uses the resolved vmid.

    def _run(self, engine_vmid, resolve_return, record=None):
        order = record if record is not None else []
        ctx = _ctx(state_path=Path("/nonexistent/state.json"), previous_state={},
                   resuming=False, engine_vmid=engine_vmid)
        with dpatch("resolve_placement",
                          side_effect=lambda *a, **k: order.append(("resolve", a[1])) or resolve_return), \
                dpatch("activate_placement", side_effect=lambda p: order.append("activate")), \
                dpatch("persist_state"), \
                dpatch("acquire_engine_lock",
                             side_effect=lambda v: order.append(("lock", v))):
            dl_placement.apply_engine_placement(ctx, Path("/nonexistent"), None, None)
        return ctx.placement, order

    def test_lock_is_taken_after_placement_and_keyed_on_the_resolved_vmid(self):
        placement, order = self._run(1234, ({"team_slots": {"team1": 0}}, None))
        # resolve -> activate (env now points at the engine's host) -> lock at that vmid
        self.assertEqual(order, [("resolve", 1234), "activate", ("lock", 1234)])
        self.assertEqual(placement, {"team_slots": {"team1": 0}})

    def test_the_engine_record_is_an_input_to_the_one_mgmt_ip_resolution(self):
        # The record used to overwrite the resolved value AFTER prepare, silently beating
        # --engine-mgmt-ip; it is now one input to the single resolution, and the flag wins.
        from deploy_lib.prepare import _resolve_engine_mgmt_ip
        from multinode.nodes_config import NodeRecord
        record = NodeRecord(name="n1", endpoint="https://n1.lab:8006", node="pve1",
                            datastore="ds", token_env="TOK", engine_mgmt_ip="10.0.0.7",
                            engine_mgmt_gw="10.0.0.1")
        ctx = _ctx(engine_vmid=1, placement={"engine_node": "n1",
                                             "nodes": {"n1": record.to_json()}})
        config.reset()
        self.addCleanup(config.reset)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_resolve_engine_mgmt_ip(ctx, None), "10.0.0.7")
            self.assertEqual(config.active().engine.mgmt_gw, "10.0.0.1")
            self.assertEqual(_resolve_engine_mgmt_ip(ctx, "10.0.0.9"), "10.0.0.9")

    def test_no_placement_means_no_activation_and_still_a_lock(self):
        _placement, order = self._run(1, (None, None))
        self.assertEqual(order, [("resolve", 1), ("lock", 1)])


class GoldenGatePosition(unittest.TestCase):
# The frozen gate must fire after the hashes, before any Proxmox mutation.

    def test_hashes_then_frozen_gate(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            golden = comp_dir / "golden.json"
            golden.write_text(json.dumps({"machines": []}))
            ctx = _ctx()
            calls = []
            with patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "pve",
                                         "TEZ_SSH_PUBLIC_KEY": "ssh-rsa AAAA"}), \
                    dpatch("generate_nakon_config", return_value=golden), \
                    dpatch("unbooted_golden_boxes", return_value=set()), \
                    dpatch("generate_stage_configs",
                                 return_value=(golden, comp_dir / "r.json",
                                               comp_dir / "f.json", None, None)), \
                    dpatch("build_nakon_bundle",
                                 side_effect=lambda p: calls.append("bundle")), \
                    dpatch("_template_vmid_map",
                                 side_effect=lambda n: calls.append("templates") or {}), \
                    dpatch("golden_hash_entries",
                                 side_effect=lambda *a, **k: calls.append("hashes")
                                 or ({"web01": {"config": {}}}, {"web01": "HASH"})), \
                    dpatch("frozen_state",
                                 return_value={"hashes": {"golden": {"web01": {"inputs": {}}}},
                                               "frozen_at": "2026-01-01 10:00:00"}), \
                    dpatch("golden_freeze_gate",
                                 side_effect=lambda *a: calls.append("gate")
                                 or {"code": True, "config": False}):
                dl_configs.generate_stage_configs_and_hashes(ctx, comp_dir)
        # the gate is the LAST thing that runs, i.e. before prepare() returns
        self.assertEqual(calls, ["bundle", "templates", "hashes", "gate"])
        self.assertEqual(ctx.golden_hashes, {"web01": "HASH"})

    def test_the_gate_sees_every_golden_box(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            golden = comp_dir / "golden.json"
            golden.write_text(json.dumps({"machines": []}))
            ctx = _ctx()
            seen = {}

            def gate(name, stored, current, frozen_at):
                seen[name] = (stored, current, frozen_at)
                return {"code": name == "keepme", "config": name == "keepme"}

            with patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "pve"}), \
                    dpatch("generate_nakon_config", return_value=golden), \
                    dpatch("unbooted_golden_boxes", return_value=set()), \
                    dpatch("generate_stage_configs",
                                 return_value=(golden, comp_dir / "r.json",
                                               comp_dir / "f.json", None, None)), \
                    dpatch("build_nakon_bundle", return_value=None), \
                    dpatch("_template_vmid_map", return_value={}), \
                    dpatch("golden_hash_entries",
                                 return_value=({"keepme": {}, "rebuildme": {}},
                                               {"keepme": "K", "rebuildme": "R"})), \
                    dpatch("frozen_state",
                                 return_value={"hashes": {"golden": {}}, "frozen_at": "t"}), \
                    dpatch("golden_freeze_gate", side_effect=gate):
                dl_configs.generate_stage_configs_and_hashes(ctx, comp_dir)
        # one classification per box, from the frozen record's stored inputs
        self.assertEqual(set(seen), {"keepme", "rebuildme"})
        self.assertEqual(seen["keepme"], ({}, {}, "t"))


class EstateInputs(unittest.TestCase):
# The document build, the stale-state guard and the engine mgmt IP/gw defaults.

    ENV = {"TEZ_SSH_PRIVATE_KEY_PATH": "/tmp/key", "TEZ_PROXMOX_NODE": "pve",
           "TEZ_PROXMOX_ENDPOINT": "https://new:8006/"}

    def _run(self, comp_dir, env=None, placement=None):
        config.reset()
        self.addCleanup(config.reset)
        ctx = _ctx(engine_vmid=1000, placement=placement)
        with patch.dict(os.environ, env if env is not None else self.ENV, clear=True):
            ctx.engine_mgmt_ip = dl_estate_inputs.engine_mgmt_ip_from_env()  # as prepare() does
            dl_estate_inputs.build_estate_inputs(ctx, comp_dir)
            # os.environ is restored when patch.dict exits, so snapshot the two engine
            # vars here — that is where the code under test exported them.
            env_view = {"engine_mgmt_ip": config.active().engine.mgmt_ip,
                        "engine_mgmt_gw": config.active().engine.mgmt_gw,
                        "marked_default":
                            "TEZ_ENGINE_MGMT_IP_IS_DEFAULT" in os.environ}
        return ctx, ctx.document(), env_view


    def _stale_gate(self, previous_state, engine_vmid=1000, from_phase=1):
    # gates.check_stale_estate_state refuses a document recorded against another host.
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, self.ENV, clear=True):
                dl_gates.check_stale_estate_state(Path(d), "probe", previous_state,
                                                  engine_vmid, from_phase)

    def test_stale_state_on_a_different_host_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            self._stale_gate({"deployed_endpoint": "https://old:8006", "scoring_vm_id": 1000})
        self.assertIn("deployed it", str(raised.exception))

    def test_stale_state_on_a_different_engine_vmid_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            self._stale_gate({"deployed_endpoint": "https://new:8006", "scoring_vm_id": 1090})
        self.assertIn("1090", str(raised.exception))




    def test_engine_mgmt_ip_defaults_to_static_and_gateway_follows(self):
        with tempfile.TemporaryDirectory() as d:
            ctx, _document, env_view = self._run(Path(d), env=dict(self.ENV))
        self.assertEqual(ctx.engine_mgmt_ip, DEFAULT_ENGINE_MGMT_IP)
        self.assertEqual(env_view["engine_mgmt_ip"], DEFAULT_ENGINE_MGMT_IP)
        self.assertEqual(env_view["engine_mgmt_gw"], DEFAULT_ENGINE_MGMT_GW)
        self.assertTrue(env_view["marked_default"])



    def test_per_run_mgmt_ip_override_beats_the_env_and_is_reexported(self):
        env = dict(self.ENV, TEZ_ENGINE_MGMT_IP="10.0.0.5")
        with patch.dict(os.environ, env, clear=True):
            ip = dl_estate_inputs.engine_mgmt_ip_from_env("10.0.0.77")
            # downstream (the engine-template build VM's ipconfig0) reads the config
            resolved = config.active().engine.mgmt_ip
            marked_default = "TEZ_ENGINE_MGMT_IP_IS_DEFAULT" in os.environ
        self.assertEqual(ip, "10.0.0.77")
        self.assertEqual(resolved, "10.0.0.77")
        self.assertFalse(marked_default)


class TargetSplit(unittest.TestCase):
    def test_unmanaged_boxes_stay_in_all_targets_but_leave_the_work_lists(self):
        # The real shape enumerate_targets returns — the roster mirror needs all of it.
        targets = [
            {"vmid": 1, "vm_name": "team1-web01", "ip": "192.168.101.2", "node": "pve",
             "slot": 0, "box_name": "web01", "team_key": "team1",
             "box": {"name": "web01", "template": "ubuntu-fix"}},
            {"vmid": 2, "vm_name": "team1-dc01", "ip": "192.168.101.3", "node": "pve",
             "slot": 0, "box_name": "dc01", "team_key": "team1",
             "box": {"name": "dc01", "template": "win-fix"}},
            {"vmid": 3, "vm_name": "team1-fw01", "ip": "192.168.101.1", "node": "pve",
             "slot": 0, "box_name": "fw01", "team_key": "team1",
             "box": {"name": "fw01", "template": "pfsense-fix", "unmanaged": True}},
        ]
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx()
            with patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "pve"}), \
                    dpatch("enumerate_targets", return_value=targets), \
                    dpatch("persist_targets") as p_persist, \
                    dpatch("is_unmanaged",
                                 side_effect=lambda b: bool(b.get("unmanaged"))), \
                    dpatch("is_windows_template",
                                 side_effect=lambda t: "win" in t):
                dl_targets.enumerate_deploy_targets(ctx, Path(d))
        self.assertEqual(ctx.node, "pve")
        self.assertEqual(ctx.all_targets, targets)
        self.assertEqual([t["vmid"] for t in ctx.managed_targets], [1, 2])
        self.assertEqual([t["vmid"] for t in ctx.linux_targets], [1])
        self.assertEqual([t["vmid"] for t in ctx.windows_targets], [2])
        p_persist.assert_called_once()


class PrepareSequencer(unittest.TestCase):
# prepare()'s step order and its early-None return, with every step stubbed.
#
#     This is the test that says the ordering the comments argue for is the ordering the
#     code runs: engine-vmid first, placement+lock next, preflight before the fresh
#     credential mint (a gate abort must not leave minted credentials no engine ever
#     received), the golden/frozen step before the confirmation (and therefore before
#     phase 1), targets only after the prompt.

    def _prepare(self, comp_dir, order, confirm=True, buf=None):
        def rec(name, setter=None):
            def run(*a, **k):
                order.append(name)
                if setter:
                    setter(*a, **k)
            return run

        def load_prior(ctx, *a, **k):
            order.append("prior")
            ctx.state_path = estate_state.state_path(comp_dir)
            ctx.previous_state = {}
            ctx.resuming = False

        with dpatch("load_competition_spec",
                          side_effect=rec("spec", lambda ctx, cd: (
                              setattr(ctx, "boxes", [{"name": "web01"}]),
                              setattr(ctx, "comp_name", "probe")))), \
                dpatch("load_prior_deploy_state", side_effect=load_prior), \
                dpatch("load_competition_inputs", side_effect=rec("inputs")), \
                dpatch("resolve_competition_teams",
                             side_effect=rec("teams", lambda ctx, *a, **k: (
                                 setattr(ctx, "state", {}),
                                 setattr(ctx, "teams", {})))), \
                dpatch("mint_competition_secrets",
                             side_effect=rec("mint")), \
                dpatch("engine_mgmt_ip_from_env",
                             return_value="10.0.0.250"), \
                dpatch("resolve_placement",
                             side_effect=lambda *a, **k: order.append("resolve")
                             or ({"version": 2, "engine_vmid": 1234, "engine_node": "n1",
                                  "nodes": {"n1": {"name": "n1",
                                                   "endpoint": "https://n1.lab:8006",
                                                   "node": "pve1", "datastore": "ds",
                                                   "token_env": "TOK"}},
                                  "slots": {"n1": 0}}, None)), \
                dpatch("activate_placement",
                             side_effect=lambda p: order.append("activate")), \
                dpatch("acquire_engine_lock",
                             side_effect=lambda v: order.append(("lock", v))), \
                dpatch("generate_stage_configs_and_hashes",
                             side_effect=rec("generated")), \
                dpatch("build_estate_inputs", side_effect=rec("estate-inputs")), \
                dpatch("run_capacity_preflight", side_effect=rec("preflight")), \
                dpatch("confirm_deploy",
                             side_effect=lambda *a, **k: order.append("confirm") or confirm), \
                dpatch("enumerate_deploy_targets", side_effect=rec("targets")), \
                dpatch("persist_state"), \
                contextlib.redirect_stdout(buf if buf is not None else io.StringIO()):
            result = deploy.prepare(comp_dir, num_teams=1, assume_yes=False, scoring_vmid=1234)
        return result

    def test_steps_run_in_pipeline_order_and_the_lock_uses_the_resolved_vmid(self):
        order = []
        with tempfile.TemporaryDirectory() as d:
            result = self._prepare(Path(d), order)
        self.assertEqual(order, ["spec", "prior", "inputs", "teams", "resolve", "activate",
                                 ("lock", 1234), "preflight", "mint", "generated",
                                 "estate-inputs", "confirm", "targets"])
        # prepare() hands back the one context the phases share, carrying what the steps set
        self.assertIsInstance(result, DeployContext)
        self.assertEqual(result.engine_vmid, 1234)
        self.assertEqual(result.comp_name, "probe")
        self.assertEqual(result.boxes_by_name, {"web01": {"name": "web01"}})
        self.assertEqual(result.reclaim_run_id, "")



class FrozenGateDoesNotClobberTheEventName(unittest.TestCase):
# Regression guard: this step READS the spec and writes only the generated fields, so the
#     competition's event name survives. Reading the spec's `name` as the loop variable
#     would assign each golden box name onto the spec and leave the LAST one there,
#     replacing the Compfile event name. The generated `event_name` — and with it the
#     ownership tag stamped on the engine and every team box — would become
#     `comp-<lastbox>` while phase 1 compares against `comp-<competition>`, so phase 1's
#     `comp_tags <= tags` ownership check would refuse to reclaim the range's own VMs;
#     `confirm_deploy` would also display the wrong name.

    def _run(self, comp_dir, frozen):
        golden = comp_dir / "golden.json"
        golden.write_text(json.dumps({"machines": []}))
        ctx = _ctx()
        with patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "pve"}), \
                dpatch("generate_nakon_config", return_value=golden), \
                dpatch("unbooted_golden_boxes", return_value=set()), \
                dpatch("generate_stage_configs",
                             return_value=(golden, comp_dir / "r.json",
                                           comp_dir / "f.json", None, None)), \
                dpatch("build_nakon_bundle", return_value=None), \
                dpatch("_template_vmid_map", return_value={}), \
                dpatch("golden_hash_entries",
                             return_value=({"web01": {}, "dc01": {}},
                                           {"web01": "H", "dc01": "H2"})), \
                dpatch("frozen_state", return_value=frozen):
            dl_configs.generate_stage_configs_and_hashes(ctx, comp_dir)
        return ctx

    def test_gate_mutates_no_spec_field(self):
    # The general invariant behind the bug: this step READS the spec and writes only
    #         into the generated fields. Any attribute mutation here is a latent version of
    #         the same clobbering bug, so guard the whole context rather than just `name`.
        with tempfile.TemporaryDirectory() as d:
            golden_dir = Path(d)
            with dpatch("frozen_state",
                              return_value={"hashes": {"golden": {}}, "frozen_at": "t"}), \
                    dpatch("golden_freeze_gate",
                                 return_value={"code": True, "config": False}):
                ctx = self._run(
                    golden_dir, {"hashes": {"golden": {}}, "frozen_at": "t"})
        self.assertEqual(ctx.name, "probe")
        self.assertEqual(ctx.comp_name, "probe")
        self.assertEqual(ctx.box_username, "ubuntu")
        self.assertEqual(ctx.difficulty, 3)
        self.assertEqual(ctx.golden_hashes, {"web01": "H", "dc01": "H2"})


if __name__ == "__main__":
    unittest.main()
