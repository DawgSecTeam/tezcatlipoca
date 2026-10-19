"""`--mode reset` — the cheapest-that-works ladder — and the hardening around it. Offline."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

import pipeline_api  # noqa: E402
from redeploy import redeploy_light_ops  # noqa: E402
from redeploy import redeploy_plant_ops  # noqa: E402
from redeploy import redeploy_rebuild_ops  # noqa: E402
from redeploy import redeploy_reset_ops  # noqa: E402
from redeploy.redeploy_context import RedeployContext  # noqa: E402

from common.constants import ownership_tags  # noqa: E402
from common import pam_bundle  # noqa: E402
from infra.ownership import clone_marker  # noqa: E402

LINUX_BOX = {"name": "web01", "template": "ubuntu-2204-web", "cpu": 2, "memory_mb": 2048}
WIN_BOX = {"name": "win01", "template": "windows-server-2022", "cpu": 2, "memory_mb": 4096}


def _target(box, vmid, team="team1", identifier=104):
    vm_name = f"team1-{box['name']}" if team == "team1" else f"{identifier}-{box['name']}"
    return {
        "box": box,
        "box_name": box["name"],
        "machine": f"{box['name']}-team{identifier}",
        "team_key": team,
        "identifier": identifier,
        "ip": f"192.168.{identifier}.{vmid % 250}",
        "vmid": vmid,
        "vm_name": vm_name,
    }


T1 = _target(LINUX_BOX, 2104)
T2 = _target(LINUX_BOX, 2104, team="team2", identifier=102)

CTX = {"ssh_key_path": "/k", "scoring_engine_ip": "10.0.0.9", "box_username": "ops"}


def _rctx(ctx=None, node="pve", comp_dir=None, state=None, teams=None, boxes=None, difficulty=None):
    return RedeployContext(comp_dir=comp_dir, name="x", comp_name="x", node=node,
                           teams=teams, boxes=boxes, difficulty=difficulty,
                           state={} if state is None else state,
                           state_path=None, ctx={} if ctx is None else ctx)


class LadderTests(unittest.TestCase):
    def _run(self, targets, snapshots, verdicts, rollback_ok=None, rebuild_ok=None,
             rollback_raise_on=(), probe_calls=None, comp_dir=None, patch_capture=True):
        calls = {"rollback": [], "rebuild": [], "probe": [], "prepare": 0}

        def fake_rollback(batch, _rctx, snapshot, _cfg, _bundle, reconfigure=False):
            calls["rollback"].append({
                "batch": [t["vm_name"] for t in batch],
                "snapshot": snapshot,
                "reconfigure": reconfigure,
            })
            if snapshot in rollback_raise_on:
                raise SystemExit("no box was rolled back successfully — nothing to do.")
            keep = rollback_ok if rollback_ok is not None else {t["vm_name"] for t in batch}
            return [t for t in batch if t["vm_name"] in keep]

        def fake_rebuild(batch, *_args):
            calls["rebuild"].append([t["vm_name"] for t in batch])
            keep = rebuild_ok if rebuild_ok is not None else {t["vm_name"] for t in batch}
            return [t for t in batch if t["vm_name"] in keep]

        def fake_probe(_ctx, t, _ports, _node):
            calls["probe"].append(t["vm_name"])
            verdict, detail = verdicts[t["vm_name"]].pop(0)
            return verdict, detail

        def fake_snaps(node, vmid):
            return list(snapshots.get(vmid, []))

        def fake_prepare(*_a):
            calls["prepare"] += 1
            return ("cfg", "bundle")

        # The PAM capture resolves the run's artifact folder and reaches the guest agent; a
        # ladder test that stubs a pam-trap verdict must touch neither.
        capture_patch = (patch.object(redeploy_reset_ops, "_capture_pam_bundle")
                         if patch_capture else nullcontext())
        with capture_patch, \
             patch.object(redeploy_reset_ops, "mode_rollback", side_effect=fake_rollback), \
             patch.object(redeploy_reset_ops, "mode_rebuild", side_effect=fake_rebuild), \
             patch.object(redeploy_reset_ops, "probe_box_health", side_effect=fake_probe), \
             patch.object(redeploy_reset_ops, "list_snapshots", side_effect=fake_snaps), \
             patch.object(redeploy_reset_ops, "prepare_nakon_assets", side_effect=fake_prepare), \
             patch.object(redeploy_reset_ops, "scored_ports_for", return_value={}):
            # mode_reset returns (fixed, levels); the ladder tests assert on `fixed`.
            out = redeploy_reset_ops.mode_reset(targets, _rctx(
                ctx=CTX, comp_dir=comp_dir or Path("competitions/x"),
                state={"box_password": "pw"}, boxes=[], difficulty="easy"))[0]
        if probe_calls is not None:
            self.assertEqual(calls["probe"], probe_calls)
        return out, calls

    def test_all_healthy_at_rung1_never_prepares_or_rebuilds(self):
        out, calls = self._run(
            [T1], {T1["vmid"]: ["tz-base", "tz-ready"]}, {T1["vm_name"]: [("healthy", "d")]})
        self.assertEqual(out, [T1])
        self.assertEqual(len(calls["rollback"]), 1)
        self.assertEqual(calls["rollback"][0]["snapshot"], "tz-ready")
        self.assertEqual(calls["rollback"][0]["reconfigure"], False)
        self.assertEqual(calls["rebuild"], [])
        self.assertEqual(calls["prepare"], 0)  # lazy assets: rung 1 needs no stage files

    def test_unhealthy_rung1_box_escalates_to_rung2_replant(self):
        verdicts = {T1["vm_name"]: [("unhealthy", "ports"), ("healthy", "d")]}
        out, calls = self._run(
            [T1], {T1["vmid"]: ["tz-base", "tz-ready"]}, verdicts, rollback_ok=[T1["vm_name"]])
        self.assertEqual(out, [T1])
        self.assertEqual([c["snapshot"] for c in calls["rollback"]], ["tz-ready", "tz-base"])
        self.assertTrue(calls["rollback"][1]["reconfigure"])
        self.assertEqual(calls["rebuild"], [])
        self.assertEqual(calls["prepare"], 1)

    # The tz-base disk is pre-plant, so the trap's documented cheap escape is the
    # replant rung; rebuild stays the last resort.
    def test_pam_trap_escalates_one_rung_not_straight_to_rebuild(self):
        verdicts = {T1["vm_name"]: [("pam-trap", "preauth"), ("healthy", "d")]}
        out, calls = self._run(
            [T1], {T1["vmid"]: ["tz-base", "tz-ready"]}, verdicts, rollback_ok=[T1["vm_name"]])
        self.assertEqual(out, [T1])
        self.assertEqual(calls["rebuild"], [])
        self.assertEqual([c["snapshot"] for c in calls["rollback"]], ["tz-ready", "tz-base"])



    def test_no_snapshots_at_all_goes_straight_to_rebuild(self):
        out, calls = self._run(
            [T1], {}, {T1["vm_name"]: [("healthy", "d")]}, rebuild_ok=[T1["vm_name"]])
        self.assertEqual(out, [T1])
        self.assertEqual(calls["rollback"], [])
        self.assertEqual(calls["rebuild"], [[T1["vm_name"]]])
        self.assertEqual(calls["prepare"], 1)

    # mode_rollback raises when NOTHING was restored; the ladder must carry on, not abort.
    def test_wholesale_rung_failure_escalates_instead_of_dying(self):
        verdicts = {T1["vm_name"]: [("healthy", "d")]}
        out, calls = self._run(
            [T1], {T1["vmid"]: ["tz-base", "tz-ready"]}, verdicts,
            rollback_ok=[T1["vm_name"]], rollback_raise_on=("tz-ready",))
        self.assertEqual(out, [T1])
        self.assertEqual([c["snapshot"] for c in calls["rollback"]], ["tz-ready", "tz-base"])

    def test_only_the_sick_box_is_rebuilt(self):
        t2 = _target(LINUX_BOX, 2105, team="team2", identifier=102)
        verdicts = {
            T1["vm_name"]: [("healthy", "d")],
            t2["vm_name"]: [("unhealthy", "a"), ("unhealthy", "b"), ("healthy", "c")],
        }
        snapshots = {T1["vmid"]: ["tz-base", "tz-ready"], t2["vmid"]: ["tz-base", "tz-ready"]}
        out, calls = self._run([T1, t2], snapshots, verdicts)
        self.assertEqual(out, [T1, t2])
        self.assertEqual(calls["rebuild"], [[t2["vm_name"]]])  # T1 was fixed at rung 1
        self.assertEqual([b for b in calls["rollback"] if b["batch"] == [t2["vm_name"]]],
                         [{"batch": [t2["vm_name"]], "snapshot": "tz-base",
                           "reconfigure": True}])

    def test_still_broken_after_all_rungs_exits_nonzero(self):
        verdicts = {T1["vm_name"]: [("unhealthy", "a")] * 3}
        with self.assertRaises(SystemExit):
            self._run([T1], {T1["vmid"]: ["tz-base", "tz-ready"]}, verdicts)


# The ladder may be the only place the trap is ever seen: capture the live disk's evidence
# before the next rung replaces it, over the guest agent (SSH is dead), and never let a
# failed capture change the escalation.
class PamCaptureHookTests(unittest.TestCase):
    def _verdicts(self):
        return {T1["vm_name"]: [("pam-trap", "preauth"), ("healthy", "ports")]}

    def test_pam_trap_captures_a_bundle_before_escalating(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(pam_bundle, "guest_agent_exec_root",
                          return_value=(0, "section output", "")) as p_exec:
            out, calls = LadderTests()._run(
                [T1], {T1["vmid"]: ["tz-base", "tz-ready"]}, self._verdicts(),
                rollback_ok=[T1["vm_name"]], comp_dir=Path(tmp), patch_capture=False)
            bundles = list((Path(tmp) / ".automated-tests").rglob("bundle.json"))
            manifests = [json.loads(p.read_text()) for p in bundles]
        self.assertEqual(out, [T1])
        self.assertEqual([c["snapshot"] for c in calls["rollback"]], ["tz-ready", "tz-base"])
        self.assertEqual(len(manifests), 1)
        self.assertTrue(p_exec.called)
        metadata = manifests[0]["metadata"]
        self.assertEqual(metadata["vmid"], T1["vmid"])
        self.assertEqual(metadata["team"], "team1")
        self.assertEqual(metadata["verdict"], "pam-trap")

    def test_a_failed_capture_does_not_change_the_ladder(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(redeploy_reset_ops, "capture_pam_bundle",
                          side_effect=RuntimeError("guest agent lost")):
            out, calls = LadderTests()._run(
                [T1], {T1["vmid"]: ["tz-base", "tz-ready"]}, self._verdicts(),
                rollback_ok=[T1["vm_name"]], comp_dir=Path(tmp), patch_capture=False)
        self.assertEqual(out, [T1])
        self.assertEqual([c["snapshot"] for c in calls["rollback"]], ["tz-ready", "tz-base"])
        self.assertEqual(calls["rebuild"], [])


# PVE rolls a disk back only to its MOST RECENT snapshot (caught live:
# "can't rollback, 'tz-base' is not most recent snapshot"). Reaching the pre-plant
# tz-base disk requires dropping the newer tz-ready first.
class RollbackOrderTests(unittest.TestCase):
    def _run(self, snapshot, snaps):
        order = []
        with patch.object(redeploy_light_ops, "list_snapshots", return_value=set(snaps)), \
             patch.object(redeploy_light_ops, "delete_snapshot",
                          side_effect=lambda *a: order.append(("delete", a[2]))), \
             patch.object(redeploy_light_ops, "rollback_snapshot",
                          side_effect=lambda *a: order.append(("rollback", a[2]))), \
             patch.object(pipeline_api, "wait_for_boxes_ssh"):
            redeploy_light_ops.mode_rollback([T1], _rctx(ctx=CTX, comp_dir=Path("c")), snapshot,
                                   None, None, reconfigure=False)
        return order

    def test_base_rollback_deletes_newer_tz_ready_before_rolling_back(self):
        order = self._run("tz-base", ["tz-base", "tz-ready"])
        self.assertEqual(order, [("delete", "tz-ready"), ("rollback", "tz-base")])

    def test_ready_rollback_touches_no_snapshots(self):
        order = self._run("tz-ready", ["tz-base", "tz-ready"])
        self.assertEqual(order, [("rollback", "tz-ready")])



class ProbeTests(unittest.TestCase):

    def setUp(self):
        # Never actually sleep between probe attempts.
        self._sleep = patch.object(redeploy_reset_ops.time, "sleep", lambda s: None)
        self._sleep.start()
        self.addCleanup(self._sleep.stop)



    def test_windows_boxes_probe_via_guest_agent(self):
        win = _target(WIN_BOX, 2105)
        with patch.object(redeploy_reset_ops, "wait_for_guest_agent", return_value=True) as p_agent, \
             patch.object(redeploy_reset_ops, "classify_ssh_failure") as p_ssh, \
             patch.object(redeploy_reset_ops, "ports_closed_from_engine", return_value=set()):
            verdict, detail = redeploy_reset_ops.probe_box_health(CTX, win, [3389], "pve")
        self.assertEqual(verdict, "healthy")
        self.assertIn("guest agent", detail)
        p_agent.assert_called_once()
        p_ssh.assert_not_called()  # Windows never reaches the bash/SSH executor

    def test_closed_ports_retry_then_report_unhealthy(self):
        with patch.object(redeploy_reset_ops, "classify_ssh_failure", return_value="ok"), \
             patch.object(redeploy_reset_ops, "ports_closed_from_engine",
                          return_value={80}) as p_ports:
            verdict, detail = redeploy_reset_ops.probe_box_health(CTX, T1, [22, 80], "pve")
        self.assertEqual(verdict, "unhealthy")
        self.assertEqual(p_ports.call_count, redeploy_reset_ops.RESET_PROBE_ATTEMPTS)
        self.assertIn("80", detail)



class PortsClosedFromEngineTests(unittest.TestCase):

    def _engine(self, stdout):
        return patch.object(redeploy_reset_ops, "ssh_on_gateway",
                            return_value=MagicMock(returncode=0, stdout=stdout))

    # P2=OK must not satisfy a P22 probe — matching is by whole token, not substring.
    def test_port_tokens_match_exactly(self):
        with self._engine("P2=CLOSED\nP22=OK\n"):
            closed = redeploy_reset_ops.ports_closed_from_engine(CTX, "192.168.104.10", [2, 22])
        self.assertEqual(closed, {2})

    def test_engine_failure_marks_every_port_closed(self):
        with patch.object(redeploy_reset_ops, "ssh_on_gateway", side_effect=RuntimeError("dead")):
            closed = redeploy_reset_ops.ports_closed_from_engine(CTX, "192.168.104.10", [22, 80])
        self.assertEqual(closed, {22, 80})



class ScoredPortsTests(unittest.TestCase):

    # Bare catalog names resolve through quotient's mapping; explicit ports win; plant_only
    # pins are unscored.
    def test_pins_resolve_to_scored_ports(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = Path(tmp)
            (comp / "box_services.json").write_text(json.dumps({
                "web01": ["apache", {"name": "score/tcp", "score_only": True,
                                     "check": "Tcp", "display": "dns", "port": 53}],
                "ftp01": [{"name": "IIS FTP", "plant_only": True}],
            }))
            ports = redeploy_reset_ops.scored_ports_for(comp)
        self.assertEqual(ports, {"web01": [53, 80]})



# mode_rebuild must stamp ownership onto the VM it recreates, not inherit a golden's
# tags (a reused golden carries a stale run-<id>).
class RebuildStampingTests(unittest.TestCase):
    def _run_rebuild(self, target, state):
        calls = []

        def fake_pve(method, path, **kw):
            calls.append((method, path, kw))
            if method == "GET" and path == "/cluster/resources":
                return {"data": [{"name": target["box"]["template"], "template": 1,
                                  "tags": "template", "vmid": 9001}]}
            if method == "GET" and path.endswith("/config"):
                return {"data": {}}
            return {"data": "task-ok"}

        comp_name = "resetcomp"
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp) / comp_name
            comp_dir.mkdir()
            env = {"TEZ_PROXMOX_NODE": "pve", "TEZ_SSH_PUBLIC_KEY": "ssh-ed25519 AAA",
                   "TEZ_PROXMOX_TEMPLATE_VM_ID": "1000", "TEZ_ENGINE_USERNAME": "ops"}
            with patch.dict(os.environ, env), \
                 patch.object(redeploy_rebuild_ops, "proxmox_api", side_effect=fake_pve), \
                 patch.object(redeploy_rebuild_ops, "destroy_vm_if_exists"), \
                 patch.object(redeploy_rebuild_ops, "wait_for_proxmox_task"), \
                 patch.object(redeploy_rebuild_ops, "stored_template_hash", return_value="abc"), \
                 patch.object(redeploy_rebuild_ops, "start_vm"), \
                 patch.object(redeploy_rebuild_ops, "take_snapshot"), \
                 patch.object(redeploy_rebuild_ops, "timed", return_value=nullcontext()), \
                 patch.object(pipeline_api, "wait_for_boxes_ssh"), \
                 patch.object(pipeline_api, "wait_for_cloud_init"), \
                 patch.object(pipeline_api, "fix_services_on_boxes"), \
                 patch.object(pipeline_api, "ensure_nat_forwarding"), \
                 patch.object(redeploy_rebuild_ops, "rerun_domain_configs", return_value=True):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    redeploy_rebuild_ops.mode_rebuild([target], _rctx(ctx=CTX, comp_dir=comp_dir, state=state),
                                          comp_dir / "cfg", comp_dir / "bundle")
        return calls, buf.getvalue(), comp_dir.name

    def test_clone_carries_the_marker_description_and_full_ownership_tags(self):
        t2 = _target(LINUX_BOX, 2104, team="team2", identifier=102)
        state = {"pipeline_version": 3, "box_password": "pw",
                 "run_id": "run-beefcafe", "golden_template_ids": {"web01": 9150}}
        calls, _out, comp_name = self._run_rebuild(t2, state)

        clone = next(kw for m, p, kw in calls if m == "POST" and p.endswith("/clone"))
        self.assertEqual(clone["data"]["description"], clone_marker(comp_name))

        put = next(kw for m, p, kw in calls if m == "PUT" and p.endswith("/config"))
        self.assertEqual(set(put["data"]["tags"].split(";")),
                         ownership_tags(comp_name, "run-beefcafe"))




# A comp whose plants are all golden-stage has an empty postclone stage, and building a
# bundle from it crashed the whole replant.
class PrepareAssetsTests(unittest.TestCase):
    def test_empty_postclone_stage_yields_no_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = Path(tmp)
            (comp / ".nakon-postclone.json").write_text(json.dumps({"machines": []}))
            cfg, bundle = redeploy_plant_ops.prepare_nakon_assets(comp, [])
        self.assertIsNone(cfg)
        self.assertIsNone(bundle)


    def _run_with_stage(self, stage_machines):
        ctx = {"ssh_key_path": "/k", "scoring_engine_ip": "10.0.0.9"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"TEZ_ENGINE_USERNAME": "ops"}), \
             patch.object(pipeline_api, "setup_ubuntu_auth"), \
             patch.object(pipeline_api, "fix_dns_on_boxes"), \
             patch.object(pipeline_api, "ensure_nat_forwarding"), \
             patch.object(pipeline_api, "run_nakon",
                          return_value=SimpleNamespace(failed=[])) as p_run, \
             patch.object(pipeline_api, "fix_services_on_boxes") as p_fix:
            stage = Path(tmp) / ".nakon-postclone.json"
            stage.write_text(json.dumps({"machines": [{"name": n} for n in stage_machines]}))
            redeploy_plant_ops.run_nakon_and_harden([T1], _rctx(ctx=ctx, comp_dir=Path(tmp)), stage, object())
        return p_run, p_fix

    # Every plant rides the golden clone, so the box is not in the post-clone stage; `nakon
    # --only` errored and the retry loop mistook it for a dead ssh session.
    def test_a_box_absent_from_a_non_empty_stage_is_not_handed_to_nakon(self):
        p_run, p_fix = self._run_with_stage(["some-other-box"])
        p_run.assert_not_called()
        p_fix.assert_called_once()

    def test_a_staged_box_is_still_planted(self):
        p_run, _p_fix = self._run_with_stage([T1["machine"], "some-other-box"])
        p_run.assert_called_once()
        self.assertEqual(p_run.call_args.kwargs["only"], [T1["machine"]])


# A reset DC wipes the AD content, so every done-marker of that team's chain is void.
class DomainMarkerCascadeTests(unittest.TestCase):
    def _markers(self, tmp, team="team1"):
        names = [f".nakon-domain-{team}-adds.json",
                 f".nakon-domain-{team}-ad-misconfigs.json",
                 f".nakon-domain-{team}-ad-accounts.json",
                 f".nakon-domain-{team}-ftp01-join.json"]
        for n in names:
            (Path(tmp) / n).write_text("{}")
        return names

    def test_dc_reset_cascades_all_team_domain_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            names = self._markers(tmp)
            (Path(tmp) / "domain_roles.json").write_text(
                json.dumps({"ad01": "dc", "ftp01": "member"}))
            (Path(tmp) / "teams.json").write_text(
                json.dumps({"team1": {"identifier": "130"}}))
            (Path(tmp) / "boxes.json").write_text(json.dumps(
                [{"name": "ad01", "template": "base-windows-server"},
                 {"name": "ftp01", "template": "base-windows-server"}]))
            with patch.object(pipeline_api, "deploy_domain_configs") as p_dom, \
                 patch.dict(os.environ, {"TEZ_ENGINE_USERNAME": "ops"}):
                settled = redeploy_plant_ops.rerun_domain_configs(
                    [_target({"name": "ad01", "template": "base-windows-server"}, 1500)],
                    _rctx(ctx=CTX, comp_dir=Path(tmp), state={"box_password": "pw"}),
                    Path(tmp) / "cfg.json")
            self.assertTrue(settled)
            remaining = [p.name for p in Path(tmp).glob(".nakon-domain-team1-*")]
            self.assertEqual(remaining, [])
            self.assertTrue(p_dom.called)
            # A DC reset re-promoted a FRESH AD: the member's "already joined"
            # self-report must be overridden or the trust is silently dead.
            self.assertTrue(p_dom.call_args.kwargs.get("force_member_join"))

    def test_member_reset_does_not_touch_the_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            names = self._markers(tmp)
            (Path(tmp) / "domain_roles.json").write_text(
                json.dumps({"ad01": "dc", "ftp01": "member"}))
            (Path(tmp) / "teams.json").write_text(json.dumps(
                {"team1": {"identifier": "130"}, "team2": {"identifier": "102"}}))
            (Path(tmp) / "boxes.json").write_text(json.dumps(
                [{"name": "ad01", "template": "base-windows-server"},
                 {"name": "ftp01", "template": "base-windows-server"}]))
            targets = [_target({"name": "ftp01", "template": "base-windows-server"}, 1501,
                               team="team2", identifier=102)]
            with patch.object(pipeline_api, "deploy_domain_configs") as p_dom, \
                 patch.dict(os.environ, {"TEZ_ENGINE_USERNAME": "ops"}):
                redeploy_plant_ops.rerun_domain_configs(targets,
                                              _rctx(ctx=CTX, comp_dir=Path(tmp),
                                                    state={"box_password": "pw"}),
                                              Path(tmp) / "cfg.json")
            remaining = [p.name for p in Path(tmp).glob(".nakon-domain-team1-*")]
            self.assertEqual(sorted(remaining), sorted(names))
            self.assertFalse(p_dom.call_args.kwargs.get("force_member_join"))


# nakon_failed_steps: append, never clobber — the deploy-time record is verify's.
class FailedStepsTests(unittest.TestCase):
    def _replant(self, state, failed):
        ctx = {"ssh_key_path": "/k", "scoring_engine_ip": "10.0.0.9"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"TEZ_ENGINE_USERNAME": "ops"}), \
             patch.object(pipeline_api, "setup_ubuntu_auth"), \
             patch.object(pipeline_api, "fix_dns_on_boxes"), \
             patch.object(pipeline_api, "ensure_nat_forwarding"), \
             patch.object(pipeline_api, "run_nakon",
                          return_value=MagicMock(failed=failed)), \
             patch.object(pipeline_api, "fix_services_on_boxes"):
            cfg = Path(tmp) / "cfg"
            cfg.write_text(json.dumps({"machines": [{"name": T1["machine"]}]}))
            redeploy_plant_ops.run_nakon_and_harden(
                [T1], _rctx(ctx=ctx, comp_dir=Path(tmp), state=state), cfg, Path(tmp) / "bundle")
        return state

    def test_replant_failures_are_appended_to_the_deploy_time_record(self):
        state = {"box_password": "pw", "nakon_failed_steps": ["deploy: mysql FAILED"]}
        state = self._replant(state, ["nginx FAILED"])
        self.assertEqual(state["nakon_failed_steps"],
                         ["deploy: mysql FAILED", "redeploy: nginx FAILED"])

    # A clean replant preserves the deploy-time record: an empty list would retroactively
    # declare the deploy's failed steps resolved.
    def test_clean_replant_keeps_the_deploy_time_record(self):
        state = {"box_password": "pw", "nakon_failed_steps": ["deploy: mysql FAILED"]}
        state = self._replant(state, [])
        self.assertEqual(state["nakon_failed_steps"], ["deploy: mysql FAILED"])


if __name__ == "__main__":
    unittest.main()
