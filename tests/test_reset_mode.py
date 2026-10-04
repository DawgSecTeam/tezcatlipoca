"""`--mode reset` — the cheapest-that-works ladder — and the hardening around it.

Covers:

* the ladder's escalation policy: a box the cheap rung fixed is never rebuilt, a box
  the cheap rung cannot fix is never left behind, a wholesale rung failure escalates
  instead of killing the reset, and PAM planted-box traps escalate one rung (the
  tz-base disk is pre-plant) rather than jumping straight to rebuild;
* the engine-vantage health probe (SSH/guest-agent + scored ports from the engine),
  including the exact-token port matching (P2 must not match P22=OK);
* mode_rebuild's ownership stamping: the clone marker rides the clone POST, the tags
  PUT carries the full run ownership set, and the terraform-drift note fires for every
  v2 box (M3.3 made all teams terraform resources), not just team1;
* nakon_failed_steps is appended to, never clobbered — the deploy-time record belongs
  to verify's plant-integrity line.

Offline; no Proxmox, no terraform, no SSH."""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "redeploy_reset_test", _REPO / "redeploy-competition.py")
redeploy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(redeploy)

from constants import ownership_tags  # noqa: E402
from range_ops import clone_marker  # noqa: E402

LINUX_BOX = {"name": "web01", "template": "ubuntu-2204-web", "cpu": 2, "memory_mb": 2048}
WIN_BOX = {"name": "win01", "template": "windows-server-2022", "cpu": 2, "memory_mb": 4096}


def _target(box, vmid, team="team1", identifier=104):
    """One (team, box) target with main.tf's naming: team1-<box>, <identifier>-<box>."""
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


class LadderTests(unittest.TestCase):
    """mode_reset escalation policy, with every rung faked."""

    def _run(self, targets, snapshots, verdicts, rollback_ok=None, rebuild_ok=None,
             rollback_raise_on=(), probe_calls=None):
        """Drive mode_reset with fakes. snapshots: {vmid: [snap names]}; verdicts:
        {vm_name: [(verdict, detail), ...] consumed in probe order}; rollback_ok /
        rebuild_ok: vm_names the fake rung reports as restored (None = all of them);
        rollback_raise_on: snapshot names that make the fake rollback raise
        (wholesale failure)."""
        calls = {"rollback": [], "rebuild": [], "probe": [], "prepare": 0}

        def fake_rollback(batch, _ctx, _node, snapshot, _comp, _state, _cfg, _bundle,
                          reconfigure=False):
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

        with patch.object(redeploy, "mode_rollback", side_effect=fake_rollback), \
             patch.object(redeploy, "mode_rebuild", side_effect=fake_rebuild), \
             patch.object(redeploy, "probe_box_health", side_effect=fake_probe), \
             patch.object(redeploy, "list_snapshots", side_effect=fake_snaps), \
             patch.object(redeploy, "prepare_nakon_assets", side_effect=fake_prepare), \
             patch.object(redeploy, "scored_ports_for", return_value={}):
            # mode_reset returns (fixed, levels); the ladder tests assert on `fixed`.
            out = redeploy.mode_reset(targets, CTX, "pve", Path("competitions/x"),
                                      {"box_password": "pw"}, {}, [], "easy")[0]
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

    def test_pam_trap_escalates_one_rung_not_straight_to_rebuild(self):
        """The tz-base disk is pre-plant, so the trap's documented cheap escape is the
        replant rung; rebuild stays the last resort."""
        verdicts = {T1["vm_name"]: [("pam-trap", "preauth"), ("healthy", "d")]}
        out, calls = self._run(
            [T1], {T1["vmid"]: ["tz-base", "tz-ready"]}, verdicts, rollback_ok=[T1["vm_name"]])
        self.assertEqual(out, [T1])
        self.assertEqual(calls["rebuild"], [])
        self.assertEqual([c["snapshot"] for c in calls["rollback"]], ["tz-ready", "tz-base"])

    def test_rung2_failure_goes_to_rebuild(self):
        verdicts = {T1["vm_name"]: [("unhealthy", "a"), ("unhealthy", "b"), ("healthy", "c")]}
        out, calls = self._run(
            [T1], {T1["vmid"]: ["tz-base", "tz-ready"]}, verdicts, rebuild_ok=[T1["vm_name"]])
        self.assertEqual(out, [T1])
        self.assertEqual(calls["rebuild"], [[T1["vm_name"]]])

    def test_no_tz_ready_snapshot_starts_at_rung2(self):
        out, calls = self._run(
            [T1], {T1["vmid"]: ["tz-base"]}, {T1["vm_name"]: [("healthy", "d")]},
            rollback_ok=[T1["vm_name"]])
        self.assertEqual(out, [T1])
        self.assertEqual([c["snapshot"] for c in calls["rollback"]], ["tz-base"])

    def test_no_snapshots_at_all_goes_straight_to_rebuild(self):
        out, calls = self._run(
            [T1], {}, {T1["vm_name"]: [("healthy", "d")]}, rebuild_ok=[T1["vm_name"]])
        self.assertEqual(out, [T1])
        self.assertEqual(calls["rollback"], [])
        self.assertEqual(calls["rebuild"], [[T1["vm_name"]]])
        self.assertEqual(calls["prepare"], 1)

    def test_wholesale_rung_failure_escalates_instead_of_dying(self):
        """mode_rollback raises when NOTHING was restored — the ladder must carry the
        batch to the next rung, not abort the reset."""
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


class RollbackOrderTests(unittest.TestCase):
    """PVE rolls a disk back only to its MOST RECENT snapshot (live-found 2026-10-03:
    "can't rollback, 'tz-base' is not most recent snapshot"). Reaching the pre-plant
    tz-base disk requires dropping the newer tz-ready first."""

    def _run(self, snapshot, snaps):
        order = []
        with patch.object(redeploy, "list_snapshots", return_value=set(snaps)), \
             patch.object(redeploy, "delete_snapshot",
                          side_effect=lambda *a: order.append(("delete", a[2]))), \
             patch.object(redeploy, "rollback_snapshot",
                          side_effect=lambda *a: order.append(("rollback", a[2]))), \
             patch.object(redeploy.pipeline_api, "wait_for_boxes_ssh"):
            redeploy.mode_rollback([T1], CTX, "pve", snapshot, Path("c"), {},
                                   None, None, reconfigure=False)
        return order

    def test_base_rollback_deletes_newer_tz_ready_before_rolling_back(self):
        order = self._run("tz-base", ["tz-base", "tz-ready"])
        self.assertEqual(order, [("delete", "tz-ready"), ("rollback", "tz-base")])

    def test_ready_rollback_touches_no_snapshots(self):
        order = self._run("tz-ready", ["tz-base", "tz-ready"])
        self.assertEqual(order, [("rollback", "tz-ready")])

    def test_base_rollback_without_tz_ready_rolls_back_directly(self):
        order = self._run("tz-base", ["tz-base"])
        self.assertEqual(order, [("rollback", "tz-base")])


class ProbeTests(unittest.TestCase):

    def setUp(self):
        # Never actually sleep between probe attempts.
        self._sleep = patch.object(redeploy.time, "sleep", lambda s: None)
        self._sleep.start()
        self.addCleanup(self._sleep.stop)

    def test_healthy_when_ssh_ok_and_ports_open(self):
        with patch.object(redeploy, "classify_ssh_failure", return_value="ok"), \
             patch.object(redeploy, "ports_closed_from_engine", return_value=set()):
            verdict, detail = redeploy.probe_box_health(CTX, T1, [22, 80], "pve")
        self.assertEqual(verdict, "healthy")
        self.assertIn("ssh", detail)

    def test_pam_trap_is_passed_through_verbatim(self):
        with patch.object(redeploy, "classify_ssh_failure", return_value="pam-trap"), \
             patch.object(redeploy, "ports_closed_from_engine") as p_ports:
            verdict, detail = redeploy.probe_box_health(CTX, T1, [22], "pve")
        self.assertEqual(verdict, "pam-trap")
        self.assertIn("PAM", detail)
        p_ports.assert_not_called()  # a trapped box is decided at SSH, not ports

    def test_windows_boxes_probe_via_guest_agent(self):
        win = _target(WIN_BOX, 2105)
        with patch.object(redeploy, "wait_for_guest_agent", return_value=True) as p_agent, \
             patch.object(redeploy, "classify_ssh_failure") as p_ssh, \
             patch.object(redeploy, "ports_closed_from_engine", return_value=set()):
            verdict, detail = redeploy.probe_box_health(CTX, win, [3389], "pve")
        self.assertEqual(verdict, "healthy")
        self.assertIn("guest agent", detail)
        p_agent.assert_called_once()
        p_ssh.assert_not_called()  # Windows never reaches the bash/SSH executor

    def test_closed_ports_retry_then_report_unhealthy(self):
        with patch.object(redeploy, "classify_ssh_failure", return_value="ok"), \
             patch.object(redeploy, "ports_closed_from_engine",
                          return_value={80}) as p_ports:
            verdict, detail = redeploy.probe_box_health(CTX, T1, [22, 80], "pve")
        self.assertEqual(verdict, "unhealthy")
        self.assertEqual(p_ports.call_count, redeploy.RESET_PROBE_ATTEMPTS)
        self.assertIn("80", detail)

    def test_ssh_unreachable_then_ok_reports_healthy(self):
        verdicts = iter(["unreachable", "unreachable", "ok"])
        with patch.object(redeploy, "classify_ssh_failure",
                          side_effect=lambda *a, **k: next(verdicts)), \
             patch.object(redeploy, "ports_closed_from_engine", return_value=set()):
            verdict, _ = redeploy.probe_box_health(CTX, T1, [], "pve")
        self.assertEqual(verdict, "healthy")


class PortsClosedFromEngineTests(unittest.TestCase):

    def _engine(self, stdout):
        return patch.object(redeploy, "ssh_to_engine",
                            return_value=MagicMock(returncode=0, stdout=stdout))

    def test_port_tokens_match_exactly(self):
        """P2=OK must not satisfy a P22 probe (or vice versa) — matching is by whole
        whitespace token, not substring."""
        with self._engine("P2=CLOSED\nP22=OK\n"):
            closed = redeploy.ports_closed_from_engine(CTX, "192.168.104.10", [2, 22])
        self.assertEqual(closed, {2})

    def test_engine_failure_marks_every_port_closed(self):
        with patch.object(redeploy, "ssh_to_engine", side_effect=RuntimeError("dead")):
            closed = redeploy.ports_closed_from_engine(CTX, "192.168.104.10", [22, 80])
        self.assertEqual(closed, {22, 80})

    def test_no_ports_never_touches_the_engine(self):
        with patch.object(redeploy, "ssh_to_engine",
                          side_effect=AssertionError("must not be called")):
            self.assertEqual(redeploy.ports_closed_from_engine(CTX, "1.2.3.4", []), set())


class ScoredPortsTests(unittest.TestCase):

    def test_pins_resolve_to_scored_ports(self):
        """Bare catalog names resolve through quotient's own mapping; explicit ports
        win; plant_only pins emit no scored check."""
        with tempfile.TemporaryDirectory() as tmp:
            comp = Path(tmp)
            (comp / "box_services.json").write_text(json.dumps({
                "web01": ["apache", {"name": "score/tcp", "score_only": True,
                                     "check": "Tcp", "display": "dns", "port": 53}],
                "ftp01": [{"name": "IIS FTP", "plant_only": True}],
            }))
            ports = redeploy.scored_ports_for(comp)
        self.assertEqual(ports, {"web01": [53, 80]})

    def test_missing_file_is_empty_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(redeploy.scored_ports_for(Path(tmp)), {})


class RebuildStampingTests(unittest.TestCase):
    """mode_rebuild must stamp ownership onto the VM it recreates, not rely on
    inheriting a golden's tags (a reused golden carries a stale run-<id>)."""

    def _run_rebuild(self, target, state):
        """Returns (recorded api calls, stdout text)."""
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
            env = {"TF_VAR_proxmox_node": "pve", "TF_VAR_ssh_public_key": "ssh-ed25519 AAA",
                   "TF_VAR_template_vm_id": "1000", "TF_VAR_vm_username": "ops"}
            with patch.dict(os.environ, env), \
                 patch.object(redeploy, "proxmox_api", side_effect=fake_pve), \
                 patch.object(redeploy, "destroy_vm_if_exists"), \
                 patch.object(redeploy, "wait_for_proxmox_task"), \
                 patch.object(redeploy, "stored_template_hash", return_value="abc"), \
                 patch.object(redeploy, "start_vm"), \
                 patch.object(redeploy, "take_snapshot"), \
                 patch.object(redeploy, "timed", return_value=nullcontext()), \
                 patch.object(redeploy.pipeline_api, "wait_for_boxes_ssh"), \
                 patch.object(redeploy.pipeline_api, "wait_for_cloud_init"), \
                 patch.object(redeploy.pipeline_api, "fix_services_on_boxes"), \
                 patch.object(redeploy.pipeline_api, "ensure_nat_forwarding"), \
                 patch.object(redeploy, "rerun_domain_configs", return_value=True):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    redeploy.mode_rebuild([target], CTX, "pve", comp_dir, state,
                                          comp_dir / "cfg", comp_dir / "bundle")
        return calls, buf.getvalue(), comp_dir.name

    def test_clone_carries_the_marker_description_and_full_ownership_tags(self):
        t2 = _target(LINUX_BOX, 2104, team="team2", identifier=102)
        for version in (2, 3):
            with self.subTest(pipeline_version=version):
                state = {"pipeline_version": version, "box_password": "pw",
                         "run_id": "run-beefcafe", "golden_template_ids": {"web01": 9150}}
                calls, _out, comp_name = self._run_rebuild(t2, state)

                clone = next(kw for m, p, kw in calls
                             if m == "POST" and p.endswith("/clone"))
                self.assertEqual(clone["data"]["description"], clone_marker(comp_name))

                put = next(kw for m, p, kw in calls if m == "PUT" and p.endswith("/config"))
                self.assertEqual(set(put["data"]["tags"].split(";")),
                                 ownership_tags(comp_name, "run-beefcafe"))

    def test_v3_range_rebuilds_from_its_golden_not_the_v1_template_path(self):
        """Live-found 2026-10-03: pipeline v3 state sent mode_rebuild down the v1
        template path, which then excluded the box template for equaling
        TF_VAR_template_vm_id (engine base == box base on this env). v3 must ride
        the golden path."""
        t2 = _target(LINUX_BOX, 2104, team="team2", identifier=102)
        state = {"pipeline_version": 3, "box_password": "pw", "run_id": "run-beefcafe",
                 "golden_template_ids": {"web01": 9150}}
        calls, _out, _name = self._run_rebuild(t2, state)
        clone = next((m, p, kw) for m, p, kw in calls if m == "POST" and p.endswith("/clone"))
        self.assertEqual(clone[1].split("/")[4], "9150")  # cloned from the golden

    def test_v2_drift_note_fires_for_non_team1_boxes(self):
        """M3.3 made every team a terraform resource; the note must say so for team2+."""
        t2 = _target(LINUX_BOX, 2104, team="team2", identifier=102)
        state = {"pipeline_version": 2, "box_password": "pw", "run_id": "run-beefcafe",
                 "golden_template_ids": {"web01": 9150}}
        _calls, out, _name = self._run_rebuild(t2, state)
        self.assertIn("Terraform-managed resource", out)

    def test_v3_drift_note_fires_for_non_team1_boxes(self):
        t2 = _target(LINUX_BOX, 2104, team="team2", identifier=102)
        state = {"pipeline_version": 3, "box_password": "pw", "run_id": "run-beefcafe",
                 "golden_template_ids": {"web01": 9150}}
        _calls, out, _name = self._run_rebuild(t2, state)
        self.assertIn("Terraform-managed resource", out)

    def test_v1_keeps_the_team1_only_drift_note(self):
        """Pre-M3.3 ranges: only team1 is terraform-managed; team2+ boxes were API
        clones, so no drift note for them."""
        t2 = _target(LINUX_BOX, 2104, team="team2", identifier=102)
        state = {"box_password": "pw"}
        _calls, out, _name = self._run_rebuild(t2, state)
        self.assertNotIn("Terraform-managed resource", out)


class PrepareAssetsTests(unittest.TestCase):
    """A comp whose plants are all golden-stage has an empty postclone stage —
    live-found 2026-10-03: building a bundle from it crashed the whole replant."""

    def test_empty_postclone_stage_yields_no_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = Path(tmp)
            (comp / ".nakon-postclone.json").write_text(json.dumps({"machines": []}))
            cfg, bundle = redeploy.prepare_nakon_assets(
                comp, {"pipeline_version": 3}, {}, [], "easy")
        self.assertIsNone(cfg)
        self.assertIsNone(bundle)

    def test_run_nakon_and_harden_skips_the_plant_pass_but_still_hardens(self):
        ctx = {"ssh_key_path": "/k", "scoring_engine_ip": "10.0.0.9"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"TF_VAR_vm_username": "ops"}), \
             patch.object(redeploy.pipeline_api, "setup_ubuntu_auth") as p_auth, \
             patch.object(redeploy.pipeline_api, "fix_dns_on_boxes"), \
             patch.object(redeploy.pipeline_api, "ensure_nat_forwarding"), \
             patch.object(redeploy.pipeline_api, "run_nakon") as p_run, \
             patch.object(redeploy.pipeline_api, "fix_services_on_boxes") as p_fix:
            redeploy.run_nakon_and_harden([T1], ctx, Path(tmp), {}, None, None)
        p_run.assert_not_called()
        p_auth.assert_called_once()
        p_fix.assert_called_once()


class DomainMarkerCascadeTests(unittest.TestCase):
    """A reset DC wipes the entire AD content — every done-marker of that team's
    domain chain is void (live-found 2026-10-03: deleting only the ADDS marker left
    svc-support missing and failed verify's domain gate)."""

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
            with patch.object(redeploy.pipeline_api, "deploy_domain_configs") as p_dom, \
                 patch.object(redeploy, "load_users_config", return_value=("ops", {})), \
                 patch.dict(os.environ, {"TF_VAR_vm_username": "ops"}):
                settled = redeploy.rerun_domain_configs(
                    [_target({"name": "ad01", "template": "base-windows-server"}, 1500)],
                    CTX, Path(tmp), {"box_password": "pw"}, Path(tmp) / "cfg.json")
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
            with patch.object(redeploy.pipeline_api, "deploy_domain_configs") as p_dom, \
                 patch.dict(os.environ, {"TF_VAR_vm_username": "ops"}):
                redeploy.rerun_domain_configs(targets, CTX, Path(tmp),
                                              {"box_password": "pw"}, Path(tmp) / "cfg.json")
            remaining = [p.name for p in Path(tmp).glob(".nakon-domain-team1-*")]
            self.assertEqual(sorted(remaining), sorted(names))
            self.assertFalse(p_dom.call_args.kwargs.get("force_member_join"))


class FailedStepsTests(unittest.TestCase):
    """nakon_failed_steps: append, never clobber."""

    def _replant(self, state, failed):
        ctx = {"ssh_key_path": "/k", "scoring_engine_ip": "10.0.0.9"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"TF_VAR_vm_username": "ops"}), \
             patch.object(redeploy.pipeline_api, "setup_ubuntu_auth"), \
             patch.object(redeploy.pipeline_api, "fix_dns_on_boxes"), \
             patch.object(redeploy.pipeline_api, "ensure_nat_forwarding"), \
             patch.object(redeploy.pipeline_api, "run_nakon",
                          return_value=MagicMock(failed=failed)), \
             patch.object(redeploy.pipeline_api, "fix_services_on_boxes"):
            redeploy.run_nakon_and_harden(
                [T1], ctx, Path(tmp), state, Path(tmp) / "cfg", Path(tmp) / "bundle")
        return state

    def test_replant_failures_are_appended_to_the_deploy_time_record(self):
        state = {"box_password": "pw", "nakon_failed_steps": ["deploy: mysql FAILED"]}
        state = self._replant(state, ["nginx FAILED"])
        self.assertEqual(state["nakon_failed_steps"],
                         ["deploy: mysql FAILED", "redeploy: nginx FAILED"])

    def test_clean_replant_keeps_the_deploy_time_record(self):
        """The old code wrote an empty list here, retroactively declaring the deploy's
        failed steps resolved."""
        state = {"box_password": "pw", "nakon_failed_steps": ["deploy: mysql FAILED"]}
        state = self._replant(state, [])
        self.assertEqual(state["nakon_failed_steps"], ["deploy: mysql FAILED"])


if __name__ == "__main__":
    unittest.main()
