"""Offline unit tests for prepare()'s extracted steps (the ten functions plus the
DeployContext assembler).

prepare() used to be a 435-line, CC-72 function; it is now a 53-line sequencer over
_load/_resolve/_apply/_build/_run/_enumerate helpers, each filling its own stage
dataclass. These tests are the call-boundary half of that split: the decidable logic of
each step (the resume guards, the golden gate's position, the stale-state guard, the
engine mgmt IP/gw defaults, the target split) and — most importantly — the ORDER the
sequencer must keep, because several of those orderings are load-bearing and commented.

Every infrastructure call is patched: none of this touches Proxmox, terraform, SSH or
the network.
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

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import deploy  # noqa: E402
from constants import (DEFAULT_ENGINE_MGMT_GW, DEFAULT_ENGINE_MGMT_IP,  # noqa: E402
                       SCORING_ENGINE_VMID)


def _spec(**overrides):
    fields = dict(name="probe", scenario="probe", difficulty=3, box_username="ubuntu",
                  credlist_usernames=["admin"], nakon_jobs=4, apt_cache=True,
                  comp_name="probe", boxes=[{"name": "web01", "template": "ubuntu-fix"}])
    fields.update(overrides)
    return deploy.CompetitionSpec(**fields)


def _secrets(**overrides):
    fields = dict(state={"last_phase": 0}, teams={"team1": {"identifier": "101", "password": "pw"}},
                  number_of_teams=1, admin_password="a", postgres_password="p",
                  redis_password="r", box_password="b", box_creds={}, domain_creds=None,
                  inject_password=None)
    fields.update(overrides)
    return deploy.CompetitionSecrets(**fields)


def _prior(comp_dir, **overrides):
    fields = dict(state_path=comp_dir / ".deploy_state.json", previous_state={}, resuming=False)
    fields.update(overrides)
    return deploy.PriorDeployState(**fields)


class EngineVmidResolution(unittest.TestCase):
    """The first step: the vmid everything else (the lock, phase-1 destroy) keys on."""

    def test_fresh_run_takes_the_scoring_vmid_flag(self):
        with tempfile.TemporaryDirectory() as d:
            identity = deploy.RunIdentity()
            deploy._resolve_engine_vmid(identity, Path(d), 1, 1234)
        self.assertEqual(identity.engine_vmid, 1234)

    def test_fresh_run_without_a_flag_takes_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            identity = deploy.RunIdentity()
            deploy._resolve_engine_vmid(identity, Path(d), 1, None)
        self.assertEqual(identity.engine_vmid, SCORING_ENGINE_VMID)

    def test_resume_reads_the_vmid_from_state_not_the_flag(self):
        # On resume state is authoritative: the engine that exists at that vmid is the
        # one the lock and phase-1 destroy must address, whatever the flag says.
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            (comp_dir / ".deploy_state.json").write_text(json.dumps({"scoring_vm_id": 4321}))
            identity = deploy.RunIdentity()
            deploy._resolve_engine_vmid(identity, comp_dir, 3, 9999)
        self.assertEqual(identity.engine_vmid, 4321)

    def test_unreadable_state_on_resume_falls_back_to_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            (comp_dir / ".deploy_state.json").write_text("{not json")
            identity = deploy.RunIdentity()
            deploy._resolve_engine_vmid(identity, comp_dir, 2, None)
        self.assertEqual(identity.engine_vmid, SCORING_ENGINE_VMID)


class PriorStateGuards(unittest.TestCase):
    """The state read + the two refusals, plus the guard_resume_from_phase wiring."""

    def _comp_dir(self, tmp, state=None):
        comp_dir = Path(tmp)
        if state is not None:
            (comp_dir / ".deploy_state.json").write_text(json.dumps(state))
        return comp_dir

    def test_resume_without_state_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            prior = _prior(Path(d))
            with self.assertRaises(SystemExit) as raised:
                deploy._load_prior_deploy_state(prior, Path(d), 2, False)
        self.assertIn("doesn't exist", str(raised.exception))

    def test_pipeline_v1_state_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = self._comp_dir(d, {"pipeline_version": 1, "last_phase": 3})
            prior = _prior(comp_dir)
            with self.assertRaises(SystemExit) as raised:
                deploy._load_prior_deploy_state(prior, comp_dir, 2, False)
        self.assertIn("pipeline v1", str(raised.exception))

    def test_guard_resume_from_phase_gets_the_states_last_completed_phase(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = self._comp_dir(d, {"pipeline_version": 2, "last_phase": 5})
            prior = _prior(comp_dir)
            with patch.object(deploy, "guard_resume_from_phase") as guard:
                deploy._load_prior_deploy_state(prior, comp_dir, 6, True)
        guard.assert_called_once_with(6, 5, comp_dir / ".deploy_state.json", force=True)

    def test_fresh_run_never_calls_the_resume_guard(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = self._comp_dir(d, {"pipeline_version": 2, "last_phase": 0})
            prior = _prior(comp_dir)
            with patch.object(deploy, "guard_resume_from_phase") as guard:
                deploy._load_prior_deploy_state(prior, comp_dir, 1, False)
        guard.assert_not_called()
        self.assertFalse(prior.resuming)


class PlacementAndLock(unittest.TestCase):
    """Placement precedes the endpoint-keyed lock, and the lock uses the resolved vmid."""

    def _run(self, identity, resolve_return, record=None):
        order = record if record is not None else []
        prior = _prior(Path("/nonexistent"), state_path=Path("/nonexistent/state.json"))
        with patch.object(deploy, "resolve_placement",
                          side_effect=lambda *a, **k: order.append(("resolve", a[1])) or resolve_return), \
                patch.object(deploy, "activate_placement", side_effect=lambda p: order.append("activate")), \
                patch.object(deploy, "write_state"), \
                patch.object(deploy, "acquire_engine_lock",
                             side_effect=lambda v: order.append(("lock", v))):
            place = deploy.EnginePlacement()
            deploy._apply_engine_placement(place, prior, _secrets(), _spec(), identity,
                                           Path("/nonexistent"), None, None)
        return place, order

    def test_lock_is_taken_after_placement_and_keyed_on_the_resolved_vmid(self):
        place, order = self._run(deploy.RunIdentity(engine_vmid=1234),
                                 ({"team_slots": {"team1": 0}}, None))
        # resolve -> activate (env now points at the engine's host) -> lock at that vmid
        self.assertEqual(order, [("resolve", 1234), "activate", ("lock", 1234)])
        self.assertEqual(place.placement, {"team_slots": {"team1": 0}})

    def test_engine_mgmt_ip_and_gw_come_from_the_resolved_engine_record(self):
        import types
        record = types.SimpleNamespace(engine_mgmt_ip="10.0.0.7", engine_mgmt_gw="10.0.0.1")
        with patch.dict(os.environ, {}, clear=True):
            _place, order = self._run(deploy.RunIdentity(engine_vmid=1),
                                      ({"team_slots": {}}, record))
            self.assertEqual(order, [("resolve", 1), "activate", ("lock", 1)])
            self.assertEqual(os.environ["TF_VAR_engine_mgmt_ip"], "10.0.0.7")
            self.assertEqual(os.environ["TF_VAR_engine_mgmt_gw"], "10.0.0.1")

    def test_no_placement_means_no_activation_and_still_a_lock(self):
        _place, order = self._run(deploy.RunIdentity(engine_vmid=1), (None, None))
        self.assertEqual(order, [("resolve", 1), ("lock", 1)])


class GoldenGatePosition(unittest.TestCase):
    """The frozen gate must fire after the hashes and BEFORE phase 1 can destroy."""

    def test_hashes_then_frozen_gate_then_frozen_keep(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            golden = comp_dir / "golden.json"
            golden.write_text(json.dumps({"machines": []}))
            generated = deploy.GeneratedConfigs()
            calls = []
            with patch.dict(os.environ, {"TF_VAR_proxmox_node": "pve",
                                         "TF_VAR_ssh_public_key": "ssh-rsa AAAA"}), \
                    patch.object(deploy, "generate_nakon_config", return_value=golden), \
                    patch.object(deploy, "unbooted_golden_boxes", return_value=set()), \
                    patch.object(deploy, "generate_stage_configs",
                                 return_value=(golden, comp_dir / "r.json",
                                               comp_dir / "f.json", None)), \
                    patch.object(deploy, "build_nakon_bundle",
                                 side_effect=lambda p: calls.append("bundle")), \
                    patch.object(deploy, "_template_vmid_map",
                                 side_effect=lambda n: calls.append("templates") or {}), \
                    patch.object(deploy, "golden_hash_entries",
                                 side_effect=lambda *a, **k: calls.append("hashes")
                                 or ({"web01": {"config": {}}}, {"web01": "HASH"})), \
                    patch.object(deploy, "frozen_state",
                                 return_value={"hashes": {"golden": {"web01": {"inputs": {}}}},
                                               "frozen_at": "2026-09-30"}), \
                    patch.object(deploy, "golden_freeze_gate",
                                 side_effect=lambda *a: calls.append("gate")
                                 or {"code": True, "config": False}):
                deploy._generate_stage_configs_and_hashes(generated, comp_dir, _spec(), _secrets())
        # the gate is the LAST thing that runs, i.e. before prepare() returns and before
        # deploy() reaches phase 1's destroy waves
        self.assertEqual(calls, ["bundle", "templates", "hashes", "gate"])
        self.assertEqual(generated.golden_hashes, {"web01": "HASH"})
        self.assertEqual(generated.frozen_keep, {"web01"})

    def test_code_drift_keeps_the_golden_and_config_drift_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            golden = comp_dir / "golden.json"
            golden.write_text(json.dumps({"machines": []}))
            generated = deploy.GeneratedConfigs()
            seen = {}

            def gate(name, stored, current, frozen_at, bundle):
                seen[name] = (stored, current, frozen_at, bundle)
                return {"code": name == "keepme", "config": name == "keepme"}

            with patch.dict(os.environ, {"TF_VAR_proxmox_node": "pve"}), \
                    patch.object(deploy, "generate_nakon_config", return_value=golden), \
                    patch.object(deploy, "unbooted_golden_boxes", return_value=set()), \
                    patch.object(deploy, "generate_stage_configs",
                                 return_value=(golden, comp_dir / "r.json",
                                               comp_dir / "f.json", None)), \
                    patch.object(deploy, "build_nakon_bundle", return_value=None), \
                    patch.object(deploy, "_template_vmid_map", return_value={}), \
                    patch.object(deploy, "golden_hash_entries",
                                 return_value=({"keepme": {}, "rebuildme": {}},
                                               {"keepme": "K", "rebuildme": "R"})), \
                    patch.object(deploy, "frozen_state",
                                 return_value={"hashes": {"golden": {}}, "frozen_at": "t"}), \
                    patch.object(deploy, "golden_freeze_gate", side_effect=gate):
                deploy._generate_stage_configs_and_hashes(generated, comp_dir, _spec(), _secrets())
        self.assertEqual(generated.frozen_keep, {"keepme"})
        self.assertEqual(set(seen), {"keepme", "rebuildme"})


class TerraformInputs(unittest.TestCase):
    """tfvars assembly, the stale-state guard and the engine mgmt IP/gw defaults."""

    ENV = {"TF_VAR_ssh_private_key_path": "/tmp/key", "TF_VAR_proxmox_node": "pve",
           "TF_VAR_proxmox_endpoint": "https://new:8006/"}

    def _run(self, comp_dir, tf_dir, env=None, prior=None, identity=None, place=None,
             from_phase=1):
        terraform = deploy.TerraformInputs()
        captured = {}
        with patch.dict(os.environ, env if env is not None else self.ENV, clear=True), \
                patch.object(deploy, "ensure_terraform_workdir", return_value=tf_dir), \
                patch.object(deploy, "update_env") as p_env, \
                patch.object(deploy, "write_text_atomic",
                             side_effect=lambda p, t: captured.__setitem__(str(p), t)), \
                patch.object(deploy, "satellite_tfvars", return_value=[{"name": "sat1"}]), \
                patch.object(deploy, "satellite_routes_for", return_value=[{"fake": True}]):
            deploy._build_terraform_inputs(
                terraform, comp_dir, _spec(), _secrets(), prior or _prior(comp_dir),
                identity or deploy.RunIdentity(engine_vmid=1000),
                place or deploy.EnginePlacement(), from_phase)
            # os.environ is restored when patch.dict exits, so snapshot the two engine
            # vars here — that is where the code under test exported them.
            env_view = {k: os.environ.get(k) for k in
                        ("TF_VAR_engine_mgmt_ip", "TF_VAR_engine_mgmt_gw")}
        return terraform, captured, p_env, env_view

    def test_tfvars_keys_and_env_copies(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            tf_dir = comp_dir / "terraform"
            tf_dir.mkdir()
            terraform, captured, p_env, _view = self._run(comp_dir, tf_dir)
        tfvars = json.loads(captured[str(tf_dir / "terraform.tfvars.json")])
        self.assertEqual(tfvars["event_name"], "probe")
        self.assertEqual(tfvars["scoring_vm_id"], 1000)
        self.assertEqual(tfvars["boxes_per_team"], [{"name": "web01", "template": "ubuntu-fix"}])
        self.assertIs(tfvars["build_team_boxes"], False)
        self.assertEqual(tfvars["golden_template_ids"], [])
        self.assertEqual(tfvars["engine_clone_id"], 0)
        self.assertNotIn("satellites", tfvars)              # single-node: no satellite keys
        env = p_env.call_args.args[0]
        self.assertEqual(env["TF_VAR_scoring_vm_id"], "1000")
        self.assertEqual(env["TF_VAR_box_username"], "ubuntu")

    def test_placement_adds_slots_and_satellite_keys(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            tf_dir = comp_dir / "terraform"
            tf_dir.mkdir()
            place = deploy.EnginePlacement(placement={"team_slots": {"team1": 2}})
            terraform, captured, _env, _view = self._run(comp_dir, tf_dir, place=place)
        tfvars = json.loads(captured[str(tf_dir / "terraform.tfvars.json")])
        self.assertEqual(tfvars["teams"]["team1"]["slot"], 2)
        self.assertEqual(tfvars["satellites"], [{"name": "sat1"}])
        self.assertEqual(tfvars["satellite_routes"], [{"fake": True}])

    def test_stale_state_on_a_different_host_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            tf_dir = comp_dir / "terraform"
            tf_dir.mkdir()
            (tf_dir / "terraform.tfstate").write_text("{}")
            prior = _prior(comp_dir, previous_state={"deployed_endpoint": "https://old:8006",
                                                     "scoring_vm_id": 1000})
            with self.assertRaises(SystemExit) as raised:
                self._run(comp_dir, tf_dir, prior=prior)
        self.assertIn("Destroy this competition first", str(raised.exception))

    def test_stale_state_on_a_different_engine_vmid_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            tf_dir = comp_dir / "terraform"
            tf_dir.mkdir()
            (tf_dir / "terraform.tfstate").write_text("{}")
            prior = _prior(comp_dir, previous_state={
                "deployed_endpoint": "https://new:8006", "scoring_vm_id": 1090})
            with self.assertRaises(SystemExit) as raised:
                self._run(comp_dir, tf_dir, prior=prior,
                          identity=deploy.RunIdentity(engine_vmid=1000))
        self.assertIn("1090", str(raised.exception))

    def test_matching_state_is_not_refused(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            tf_dir = comp_dir / "terraform"
            tf_dir.mkdir()
            (tf_dir / "terraform.tfstate").write_text("{}")
            prior = _prior(comp_dir, previous_state={
                "deployed_endpoint": "https://new:8006", "scoring_vm_id": 1000})
            terraform, captured, _env, _view = self._run(comp_dir, tf_dir, prior=prior)
        self.assertIn(str(tf_dir / "terraform.tfvars.json"), captured)

    def test_engine_mgmt_ip_defaults_to_static_and_gateway_follows(self):
        env = dict(self.ENV)
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            tf_dir = comp_dir / "terraform"
            tf_dir.mkdir()
            terraform, _captured, _env, env_view = self._run(comp_dir, tf_dir, env=env)
        self.assertEqual(terraform.engine_mgmt_ip, DEFAULT_ENGINE_MGMT_IP)
        self.assertEqual(env_view["TF_VAR_engine_mgmt_ip"], DEFAULT_ENGINE_MGMT_IP)
        self.assertEqual(env_view["TF_VAR_engine_mgmt_gw"], DEFAULT_ENGINE_MGMT_GW)

    def test_explicit_empty_mgmt_ip_keeps_dhcp_but_still_defaults_the_gateway(self):
        env = dict(self.ENV, TF_VAR_engine_mgmt_ip="")
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            tf_dir = comp_dir / "terraform"
            tf_dir.mkdir()
            terraform, _captured, _env, env_view = self._run(comp_dir, tf_dir, env=env)
        self.assertEqual(terraform.engine_mgmt_ip, "")
        self.assertEqual(env_view["TF_VAR_engine_mgmt_gw"], DEFAULT_ENGINE_MGMT_GW)

    def test_explicit_gateway_is_not_overwritten(self):
        env = dict(self.ENV, TF_VAR_engine_mgmt_ip="10.0.0.5", TF_VAR_engine_mgmt_gw="10.9.9.9")
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            tf_dir = comp_dir / "terraform"
            tf_dir.mkdir()
            terraform, _captured, _env, env_view = self._run(comp_dir, tf_dir, env=env)
        self.assertEqual(terraform.engine_mgmt_ip, "10.0.0.5")
        self.assertEqual(env_view["TF_VAR_engine_mgmt_gw"], "10.9.9.9")


class TargetSplit(unittest.TestCase):
    def test_unmanaged_boxes_stay_in_all_targets_but_leave_the_work_lists(self):
        targets = [
            {"vmid": 1, "box": {"name": "web01", "template": "ubuntu-fix"}},
            {"vmid": 2, "box": {"name": "dc01", "template": "win-fix"}},
            {"vmid": 3, "box": {"name": "fw01", "template": "pfsense-fix", "unmanaged": True}},
        ]
        with tempfile.TemporaryDirectory() as d:
            stage = deploy.DeployTargets()
            with patch.dict(os.environ, {"TF_VAR_proxmox_node": "pve"}), \
                    patch.object(deploy, "enumerate_targets", return_value=targets), \
                    patch.object(deploy, "persist_targets") as p_persist, \
                    patch.object(deploy, "is_unmanaged",
                                 side_effect=lambda b: bool(b.get("unmanaged"))), \
                    patch.object(deploy, "is_windows_template",
                                 side_effect=lambda t: "win" in t):
                deploy._enumerate_deploy_targets(stage, Path(d), _spec(), _secrets(),
                                                 deploy.EnginePlacement())
        self.assertEqual(stage.node, "pve")
        self.assertEqual(stage.all_targets, targets)
        self.assertEqual([t["vmid"] for t in stage.managed_targets], [1, 2])
        self.assertEqual([t["vmid"] for t in stage.linux_targets], [1])
        self.assertEqual([t["vmid"] for t in stage.windows_targets], [2])
        p_persist.assert_called_once()


class PrepareSequencer(unittest.TestCase):
    """prepare()'s step order and its early-None return, with every step stubbed.

    This is the test that says the ordering the comments argue for is the ordering the
    code runs: engine-vmid first, placement+lock next, the golden/frozen step before the
    confirmation (and therefore before phase 1), targets only after the prompt.
    """

    def _prepare(self, comp_dir, order, confirm=True, buf=None):
        def rec(name, setter=None):
            def run(*a, **k):
                order.append(name)
                if setter:
                    setter(*a, **k)
            return run

        def load_prior(prior, *a, **k):
            order.append("prior")
            prior.state_path = comp_dir / ".deploy_state.json"
            prior.previous_state = {}
            prior.resuming = False

        with patch.object(deploy, "_load_competition_spec",
                          side_effect=rec("spec", lambda spec, cd: (
                              setattr(spec, "boxes", [{"name": "web01"}]),
                              setattr(spec, "comp_name", "probe")))), \
                patch.object(deploy, "_load_prior_deploy_state", side_effect=load_prior), \
                patch.object(deploy, "_load_competition_inputs", side_effect=rec("inputs")), \
                patch.object(deploy, "_resolve_competition_secrets",
                             side_effect=rec("secrets", lambda secrets, *a, **k: (
                                 setattr(secrets, "state", {}),
                                 setattr(secrets, "teams", {})))), \
                patch.object(deploy, "resolve_placement",
                             side_effect=lambda *a, **k: order.append("resolve")
                             or ({"team_slots": {}}, None)), \
                patch.object(deploy, "activate_placement",
                             side_effect=lambda p: order.append("activate")), \
                patch.object(deploy, "acquire_engine_lock",
                             side_effect=lambda v: order.append(("lock", v))), \
                patch.object(deploy, "_generate_stage_configs_and_hashes",
                             side_effect=rec("generated")), \
                patch.object(deploy, "_build_terraform_inputs", side_effect=rec("terraform")), \
                patch.object(deploy, "_run_competition_preflight", side_effect=rec("preflight")), \
                patch.object(deploy, "confirm_deploy",
                             side_effect=lambda *a, **k: order.append("confirm") or confirm), \
                patch.object(deploy, "_enumerate_deploy_targets", side_effect=rec("targets")), \
                patch.object(deploy, "_assemble_deploy_context",
                             side_effect=lambda *a, **k: order.append("assemble") or "CTX"), \
                patch.object(deploy, "write_state"), \
                contextlib.redirect_stdout(buf if buf is not None else io.StringIO()):
            result = deploy.prepare(comp_dir, num_teams=1, assume_yes=False, scoring_vmid=1234)
        return result

    def test_steps_run_in_pipeline_order_and_the_lock_uses_the_resolved_vmid(self):
        order = []
        with tempfile.TemporaryDirectory() as d:
            result = self._prepare(Path(d), order)
        self.assertEqual(result, "CTX")
        self.assertEqual(order, ["spec", "prior", "inputs", "secrets", "resolve", "activate",
                                 ("lock", 1234), "generated", "terraform", "preflight",
                                 "confirm", "targets", "assemble"])

    def test_declining_the_prompt_returns_none_and_runs_nothing_after_it(self):
        order = []
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            result = self._prepare(Path(d), order, confirm=False, buf=buf)
        self.assertIsNone(result)
        self.assertIn("Deployment cancelled.", buf.getvalue())
        # no target enumeration, no context: deploy() sees None and runs no phase
        self.assertEqual(order, ["spec", "prior", "inputs", "secrets", "resolve", "activate",
                                 ("lock", 1234), "generated", "terraform", "preflight",
                                 "confirm"])


class FrozenGateDoesNotClobberTheEventName(unittest.TestCase):
    """Regression: a frozen run must not overwrite the competition's event name.

    The loop that runs the per-golden frozen gate used to read
    `for spec.name in generated.golden_hashes`, which assigned each golden box name
    onto the spec and left the LAST one there. On a frozen competition that silently
    replaced the Compfile event name, so `terraform.tfvars.json`'s `event_name` — and
    with it `local.comp_tag`, the ownership tag stamped on the engine and every team
    box — became `comp-<lastbox>` while phase 1 compares against `comp-<competition>`.
    Phase 1's `comp_tags <= tags` ownership check then FAILED and refused to reclaim
    the range's own VMs; `confirm_deploy` also displayed the wrong name.
    """

    def _run(self, comp_dir, frozen):
        golden = comp_dir / "golden.json"
        golden.write_text(json.dumps({"machines": []}))
        generated = deploy.GeneratedConfigs()
        with patch.dict(os.environ, {"TF_VAR_proxmox_node": "pve"}), \
                patch.object(deploy, "generate_nakon_config", return_value=golden), \
                patch.object(deploy, "unbooted_golden_boxes", return_value=set()), \
                patch.object(deploy, "generate_stage_configs",
                             return_value=(golden, comp_dir / "r.json",
                                           comp_dir / "f.json", None)), \
                patch.object(deploy, "build_nakon_bundle", return_value=None), \
                patch.object(deploy, "_template_vmid_map", return_value={}), \
                patch.object(deploy, "golden_hash_entries",
                             return_value=({"web01": {}, "dc01": {}},
                                           {"web01": "H", "dc01": "H2"})), \
                patch.object(deploy, "frozen_state", return_value=frozen):
            spec = _spec()
            deploy._generate_stage_configs_and_hashes(generated, comp_dir, spec, _secrets())
        return spec, generated

    def test_frozen_run_keeps_the_event_name(self):
        with tempfile.TemporaryDirectory() as d:
            spec, _ = self._run(Path(d), {"hashes": {"golden": {}}, "frozen_at": "t"})
        self.assertEqual(spec.name, "probe",
                         "the frozen gate clobbered spec.name — terraform would tag the "
                         "range comp-<lastbox> while phase 1 looks for comp-<competition>")

    def test_frozen_gate_still_runs_over_every_box(self):
        """The fix must not skip the gate it was guarding."""
        calls = []

        def gate(name, *a, **kw):
            calls.append(name)
            return {"code": False, "config": False}

        with tempfile.TemporaryDirectory() as d:
            with patch.object(deploy, "golden_freeze_gate", side_effect=gate):
                spec, _ = self._run(Path(d), {"hashes": {"golden": {}}, "frozen_at": "t"})
        self.assertEqual(sorted(calls), ["dc01", "web01"])
        self.assertEqual(spec.name, "probe")

    def test_unfrozen_run_leaves_the_event_name_alone(self):
        with tempfile.TemporaryDirectory() as d:
            spec, _ = self._run(Path(d), None)
        self.assertEqual(spec.name, "probe")

    def test_gate_mutates_no_spec_field(self):
        """The general invariant behind the bug: this step READS the spec and writes only
        into `generated`. Any attribute mutation here is a latent version of the same
        clobbering bug, so guard the whole dataclass rather than just `name`."""
        with tempfile.TemporaryDirectory() as d:
            golden_dir = Path(d)
            with patch.object(deploy, "frozen_state",
                              return_value={"hashes": {"golden": {}}, "frozen_at": "t"}), \
                    patch.object(deploy, "golden_freeze_gate",
                                 return_value={"code": True, "config": False}):
                spec, generated = self._run(
                    golden_dir, {"hashes": {"golden": {}}, "frozen_at": "t"})
        self.assertEqual(spec.name, "probe")
        self.assertEqual(spec.comp_name, "probe")
        self.assertEqual(spec.box_username, "ubuntu")
        self.assertEqual(spec.difficulty, 3)
        # code drift IS still collected into `generated` — the gate did its job
        self.assertEqual(generated.frozen_keep, {"web01", "dc01"})


if __name__ == "__main__":
    unittest.main()
