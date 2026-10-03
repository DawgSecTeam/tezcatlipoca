"""Per-deploy run-identity ownership (2026-10-02 near-miss).

Two worktrees deployed the SAME competition ID concurrently; every destruction
guard matched on `comp-<name>` tags alone, so either session's phase 1 or teardown
could destroy the other's VMs. The fix: a run id minted per competition directory,
stamped on everything the deploy creates, and required by every destruction path.
These tests pin the fail-closed behavior: an untagged VM, a same-comp VM from a
DIFFERENT run, and a foreign VM all survive; only the full ownership set is
touchable. Offline; fake Proxmox API."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import range_ops  # noqa: E402

RUN = "run-abcdef01"
OTHER_RUN = "run-12345678"
COMP = "c1"
OURS = f"tezcatlipoca,comp-{COMP},{RUN}"
OTHER = f"tezcatlipoca,comp-{COMP},{OTHER_RUN}"


def load_destroy():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "destroy_competition_ownership", _REPO / "destroy-competition.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


destroy = load_destroy()
import deploy  # noqa: E402
import config_ops  # noqa: E402
import nodes_ops  # noqa: E402


class FakePVE:
    """Minimal PVE: node VM listings, per-VM configs, DELETE/PUT capture."""

    def __init__(self, nodes=None, cfgs=None, listing_fails=()):
        self.nodes = nodes or {}          # node -> [vm dicts]
        self.cfgs = cfgs or {}            # vmid -> config dict
        self.listing_fails = set(listing_fails)
        self.deleted = []
        self.puts = []

    def __call__(self, method, path, **kw):
        if method == "GET" and path.endswith("/qemu"):
            node = path.split("/nodes/")[1].split("/")[0]
            if node in self.listing_fails:
                raise ConnectionError(f"{node} unreachable")
            return {"data": self.nodes.get(node, [])}
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


@patch.object(range_ops, "wait_for_proxmox_task", lambda *a, **k: None)
class DestroyVmOwnership(unittest.TestCase):
    """The shared delete primitive: only proven ownership is touchable."""

    def test_untagged_vm_is_refused_by_default(self):
        fake = FakePVE(nodes={"n": [vm(4150, "mystery-box", "")]},
                       cfgs={4150: {}})
        with patch.object(range_ops, "proxmox_api", fake):
            with self.assertRaisesRegex(RuntimeError, "UNTAGGED vmid 4150.*escape hatch"):
                range_ops.destroy_vm_if_exists("n", 4150, expect_tags={RUN, "tezcatlipoca"})
        self.assertEqual(fake.deleted, [])

    def test_untagged_vm_destroyed_only_with_explicit_allowance(self):
        fake = FakePVE(nodes={"n": [vm(4150, "mystery-box", "")]},
                       cfgs={4150: {}})
        with patch.object(range_ops, "proxmox_api", fake):
            range_ops.destroy_vm_if_exists("n", 4150, expect_tags={RUN, "tezcatlipoca"},
                                           allow_untagged=True)
        self.assertEqual(fake.deleted, [4150])

    def test_same_comp_different_run_is_refused_with_legacy_hint(self):
        fake = FakePVE(nodes={"n": [vm(4150, "someone-elses-box", OTHER)]},
                       cfgs={4150: {"tags": OTHER}})
        with patch.object(range_ops, "proxmox_api", fake):
            with self.assertRaisesRegex(RuntimeError, "PRE-RUN-ID deploy|--legacy-tags"):
                range_ops.destroy_vm_if_exists("n", 4150, expect_tags={RUN, "tezcatlipoca"})
            # The hint fires for comp-tagged VMs missing the run tag specifically.
            with self.assertRaisesRegex(RuntimeError, "PRE-RUN-ID deploy"):
                range_ops.destroy_vm_if_exists(
                    "n", 4150, expect_tags={"tezcatlipoca", f"comp-{COMP}", RUN})
        self.assertEqual(fake.deleted, [])

    def test_full_ownership_set_passes(self):
        fake = FakePVE(nodes={"n": [vm(4150, "ours", OURS)]},
                       cfgs={4150: {"tags": OURS}})
        with patch.object(range_ops, "proxmox_api", fake):
            range_ops.destroy_vm_if_exists(
                "n", 4150, expect_tags={"tezcatlipoca", f"comp-{COMP}", RUN})
        self.assertEqual(fake.deleted, [4150])


class LeftoverSweep(unittest.TestCase):
    """sweep_tagged_leftovers: run-id scoped, skipped without proof, legacy opt-in."""

    def test_run_scoped_sweep_leaves_other_runs_alone(self):
        fake = FakePVE(nodes={"n": [vm(2300, "ours", OURS),
                                    vm(2313, "other-run", OTHER, status="running"),
                                    vm(1080, "foreign", "tezcatlipoca;comp-other")]})
        with patch.object(destroy, "proxmox_api", fake), \
                patch.object(destroy, "wait_for_proxmox_task"):
            destroy.sweep_tagged_leftovers(["n"], COMP, run_id=RUN)
        self.assertEqual(fake.deleted, [2300])

    def test_no_run_id_and_no_flag_skips_the_sweep(self):
        fake = FakePVE(nodes={"n": [vm(2300, "comp-tagged", OTHER)]})
        out = io.StringIO()
        with patch.object(destroy, "proxmox_api", fake), \
                contextlib.redirect_stdout(out):
            destroy.sweep_tagged_leftovers(["n"], COMP)
        self.assertEqual(fake.deleted, [])
        self.assertIn("SKIPPED", out.getvalue())
        self.assertIn("--legacy-tags", out.getvalue())

    def test_legacy_flag_sweeps_by_comp_tags_with_a_warning(self):
        fake = FakePVE(nodes={"n": [vm(2300, "comp-tagged", OTHER)]})
        out = io.StringIO()
        with patch.object(destroy, "proxmox_api", fake), \
                patch.object(destroy, "wait_for_proxmox_task"), \
                contextlib.redirect_stdout(out):
            destroy.sweep_tagged_leftovers(["n"], COMP, legacy=True)
        self.assertEqual(fake.deleted, [2300])
        self.assertIn("WARNING", out.getvalue())

    def test_partial_overlap_is_never_a_candidate(self):
        fake = FakePVE(nodes={"n": [vm(1080, "foreign", "tezcatlipoca;comp-other"),
                                    vm(1333, "golden", "template")]})
        with patch.object(destroy, "proxmox_api", fake), \
                patch.object(destroy, "wait_for_proxmox_task"):
            destroy.sweep_tagged_leftovers(["n"], COMP, run_id=RUN)
        self.assertEqual(fake.deleted, [])


class PreStopOwnership(unittest.TestCase):
    """pre_stop_windows_boxes: a name match alone must not hard-stop another run."""

    TEAMS = {"team1": {"identifier": "101", "password": "x"}}
    BOXES = [{"name": "web01"}]

    def test_other_run_same_name_is_not_stopped(self):
        fake = FakePVE(nodes={"n": [vm(301, "101-web01", OTHER, status="running"),
                                    vm(302, "101-web01", OURS, status="running")]})
        stopped = []
        with patch.object(destroy, "proxmox_api", fake), \
                patch.object(destroy, "wait_for_proxmox_task",
                             side_effect=lambda n, u: stopped.append(n)):
            destroy.pre_stop_windows_boxes(self.TEAMS, self.BOXES, "n",
                                           expect_tags={"tezcatlipoca", f"comp-{COMP}", RUN})
        self.assertEqual(stopped, ["n"])

    def test_no_tags_requires_only_comp_tags(self):
        # Legacy state: comp-tag gate (today's semantics), name still must match.
        fake = FakePVE(nodes={"n": [vm(301, "101-web01", f"tezcatlipoca,comp-{COMP}",
                                       status="running")]})
        stopped = []
        with patch.object(destroy, "proxmox_api", fake), \
                patch.object(destroy, "wait_for_proxmox_task",
                             side_effect=lambda n, u: stopped.append(n)):
            destroy.pre_stop_windows_boxes(self.TEAMS, self.BOXES, "n",
                                           expect_tags={"tezcatlipoca", f"comp-{COMP}"})
        self.assertEqual(stopped, ["n"])


class CloneMapFailClosed(unittest.TestCase):
    """destroy_cloned_vms: a failed node listing must never purge blind."""

    def test_listing_failure_skips_entries(self):
        fake = FakePVE(nodes={"n": [vm(101, "web01", "")]}, listing_fails={"n"})
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cloned_vms.json"
            path.write_text(json.dumps({"web01": {"vmid": 101, "node": "n"}}))
            out = io.StringIO()
            with patch.object(destroy, "proxmox_api", fake), \
                    patch.object(destroy, "wait_for_proxmox_task"), \
                    contextlib.redirect_stdout(out):
                destroy.destroy_cloned_vms(path, "n")
        self.assertEqual(fake.deleted, [])
        self.assertIn("NOT deleting without a live check", out.getvalue())


class ReportRemainingClassification(unittest.TestCase):
    def test_ours_other_run_and_legacy_are_distinguished(self):
        fake = FakePVE(nodes={"n": [vm(2300, "ours", OURS),
                                    vm(2313, "other-run", OTHER),
                                    vm(2314, "legacy", f"tezcatlipoca,comp-{COMP}")]})
        out = io.StringIO()
        with patch.object(destroy, "proxmox_api", fake), \
                contextlib.redirect_stdout(out):
            destroy.report_remaining(["n"], COMP, {"team1": {"identifier": "101"}},
                                     run_id=RUN)
        text = out.getvalue()
        self.assertIn("OURS", text)
        self.assertIn("DIFFERENT run", text)
        self.assertIn("no run id recorded", text)


class RetagOwnership(unittest.TestCase):
    def test_missing_run_tag_is_added(self):
        fake = FakePVE(cfgs={4150: {"tags": f"tezcatlipoca,comp-{COMP}"}})
        with patch.object(range_ops, "proxmox_api", fake):
            range_ops.retag_ownership("n", 4150,
                                      {"tezcatlipoca", f"comp-{COMP}", RUN})
        self.assertEqual(fake.puts[0][1]["tags"], f"comp-{COMP};run-abcdef01;tezcatlipoca")

    def test_current_tags_are_a_noop(self):
        fake = FakePVE(cfgs={4150: {"tags": OURS}})
        with patch.object(range_ops, "proxmox_api", fake):
            range_ops.retag_ownership("n", 4150,
                                      {"tezcatlipoca", f"comp-{COMP}", RUN})
        self.assertEqual(fake.puts, [])

    def test_foreign_vm_is_never_retagged(self):
        fake = FakePVE(cfgs={4150: {"tags": "tezcatlipoca;comp-something-else"}})
        out = io.StringIO()
        with patch.object(range_ops, "proxmox_api", fake), \
                contextlib.redirect_stdout(out):
            range_ops.retag_ownership("n", 4150,
                                      {"tezcatlipoca", f"comp-{COMP}", RUN})
        self.assertEqual(fake.puts, [])
        self.assertIn("not provably this competition's", out.getvalue())


class DeployContextTags(unittest.TestCase):
    def _ctx(self, **kw):
        fields = dict(
            comp_dir=Path("."), comp_name=COMP, state_path=Path("."), from_phase=1,
            resuming=False, assume_yes=True, name=COMP, scenario="", box_username="ubuntu",
            credlist_usernames=[], nakon_jobs=4, apt_cache=True, injects=[], packet_pw=None,
            state={}, teams={}, number_of_teams=0, admin_password="a", scoring_password="s",
            postgres_password="p",
            redis_password="r", box_password="b", box_creds={}, domain_creds=None,
            inject_password=None, placement=None, node="pve", engine_vmid=1000,
            engine_mgmt_ip="10.0.0.1", boxes=[], boxes_by_name={}, unbooted=set(),
            nakon_config_path=Path("n.json"), golden_config_path=Path("g.json"),
            repair_config_path=Path("r.json"), final_config_path=Path("f.json"),
            golden_inputs={}, golden_hashes={}, frozen_keep=set(), tf_dir=Path("."),
            tfvars_path=Path("."), tfvars={}, ssh_key_abs="/k", all_targets=[],
            managed_targets=[], linux_targets=[], windows_targets=[])
        fields.update(kw)
        return deploy.DeployContext(**fields)

    def test_creation_tags_carry_the_run_id(self):
        ctx = self._ctx(run_id=RUN)
        self.assertEqual(ctx.comp_tags, {"tezcatlipoca", f"comp-{COMP}", RUN})

    def test_reclaim_tags_prefer_the_prior_run_id(self):
        ctx = self._ctx(run_id=RUN, reclaim_run_id=OTHER_RUN)
        self.assertEqual(ctx.reclaim_tags, {"tezcatlipoca", f"comp-{COMP}", OTHER_RUN})
        self.assertEqual(ctx.reclaim_tag, OTHER_RUN)

    def test_reclaim_falls_back_to_the_current_run_id(self):
        ctx = self._ctx(run_id=RUN)
        self.assertEqual(ctx.reclaim_tag, RUN)


class RunIdMinting(unittest.TestCase):
    """_resolve_competition_secrets: minted once per comp dir, reused forever after."""

    def _spec(self):
        return deploy.CompetitionSpec(comp_name=COMP, credlist_usernames=[])

    def test_fresh_deploy_mints_a_run_id_into_state(self):
        with tempfile.TemporaryDirectory() as d:
            prior = deploy.PriorDeployState(state_path=Path(d) / "s.json",
                                            previous_state={}, resuming=False)
            secrets = deploy.CompetitionSecrets()
            with patch.object(deploy, "write_state"), \
                    patch.object(deploy, "collect_teams",
                                 return_value={"team1": {"identifier": "101",
                                                         "password": "x"}}):
                deploy._resolve_competition_secrets(
                    secrets, prior, self._spec(), deploy.CompetitionInputs(), 1,
                    deploy.RunIdentity(engine_vmid=1000), 1)
        self.assertRegex(secrets.run_id, r"^run-[0-9a-f]{8}$")
        self.assertEqual(secrets.state["run_id"], secrets.run_id)

    def test_resume_reuses_the_prior_run_id(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = Path(d) / ".deploy_state.json"
            state_path.write_text(json.dumps({
                "run_id": RUN, "last_phase": 3,
                "teams": {"team1": {"identifier": "101", "password": "x"}}}))
            prior = deploy.PriorDeployState(state_path=state_path,
                                            previous_state={"run_id": RUN}, resuming=True)
            secrets = deploy.CompetitionSecrets()
            with patch.object(deploy, "write_state"):
                deploy._resolve_competition_secrets(
                    secrets, prior, self._spec(), deploy.CompetitionInputs(), None,
                    deploy.RunIdentity(engine_vmid=1000), 4)
        self.assertEqual(secrets.run_id, RUN)
        self.assertEqual(secrets.state["run_id"], RUN)


class TfvarsCarryRunTag(unittest.TestCase):
    def test_run_tag_is_written_to_tfvars(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            tf_dir = comp_dir / "terraform"
            tf_dir.mkdir()
            secrets = deploy.CompetitionSecrets(run_id=RUN, teams={}, number_of_teams=0)
            terraform = deploy.TerraformInputs()
            captured = {}
            env = {"TF_VAR_ssh_private_key_path": "/tmp/key",
                   "TF_VAR_proxmox_node": "pve",
                   "TF_VAR_proxmox_endpoint": "https://pve:8006/"}
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(deploy, "ensure_terraform_workdir", return_value=tf_dir), \
                    patch.object(deploy, "update_env"), \
                    patch.object(deploy, "write_text_atomic",
                                 side_effect=lambda p, t: captured.__setitem__(str(p), t)):
                deploy._build_terraform_inputs(
                    terraform, comp_dir,
                    deploy.CompetitionSpec(comp_name=COMP, boxes=[{"name": "web01",
                                                                   "template": "t"}]),
                    secrets, deploy.PriorDeployState(), deploy.RunIdentity(1000),
                    deploy.EnginePlacement(), 1)
        tfvars = json.loads(captured[str(tf_dir / "terraform.tfvars.json")])
        self.assertEqual(tfvars["run_tag"], RUN)


class MultinodePreflightRunAware(unittest.TestCase):
    """A same-comp VM without the run tag is a COLLISION, not 'ours'."""

    PLACEMENT = {
        "engine_node": "n1", "team_nodes": {"team1": "n1"},
        "satellites": [], "nodes": {"n1": {}}, "slots": {"n1": 0},
        "team_slots": {"team1": 0},
        "team_identifiers": {"team1": "101"},
    }
    TEAMS = {"team1": {"identifier": "101"}}

    def _run(self, engine_tags):
        boxes = [{"name": "web01", "template": "ubuntu-fix", "memory_mb": 512}]
        colliding = dict(vm(1000, "quotient-engine", engine_tags, status="running"),
                         node="n1")
        base_template = dict(vm(900, "ubuntu-fix", "template"), node="n1")
        base_template["template"] = 1
        with patch.dict(os.environ, {"TOK_N1": "t"}), \
                patch.object(nodes_ops, "record_of",
                             side_effect=lambda pl, n: SimpleNamespace(
                                 node=n, engine_base_vmid=900, datastore="local-zfs")), \
                patch.object(nodes_ops, "teams_on_node",
                             side_effect=lambda pl, n: [k for k, v in
                                                        pl["team_nodes"].items() if v == n]), \
                patch.object(range_ops, "cluster_vms_for",
                             return_value=[colliding, base_template]), \
                patch.object(config_ops, "has_clone_marker", return_value=False), \
                patch.object(config_ops, "proxmox_api",
                             return_value={"data": {"ostype": "l26",
                                                    "ide2": "local:vm-900-cloudinit"}}), \
                patch.object(config_ops, "_catalog_gate"):
            config_ops.preflight_gates_multinode(
                Path("competitions/example"), boxes, self.TEAMS, 1000, self.PLACEMENT,
                engine_mgmt_ip=None, check_free=True, our_run_tag=RUN)

    def test_same_comp_tag_without_run_tag_is_a_collision(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run("tezcatlipoca,comp-example")
        self.assertIn("--legacy-tags", str(ctx.exception))

    def test_full_ownership_match_is_ours(self):
        # The engine tagged with the FULL set is this deploy's leftover: phase 1
        # reclaims it, so preflight must not refuse.
        self._run(f"tezcatlipoca,comp-example,{RUN}")  # must not raise


if __name__ == "__main__":
    unittest.main()
