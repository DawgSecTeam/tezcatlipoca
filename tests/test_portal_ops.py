"""Portal deploy side (portal_ops): portal.json, tunnel ownership, bundle, phase-8 hook."""

import contextlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import portal_ops  # noqa: E402

TEAMS = {"team1": {"identifier": "101", "password": "a"},
         "team2": {"identifier": "102", "password": "b"}}
BOXES = [{"name": "web01", "template": "ubuntu24.04-fix", "last_octet": 10},
         {"name": "dc01", "template": "windows-server-2022", "last_octet": 20},
         {"name": "fw01", "template": "pfsense", "last_octet": 1, "unmanaged": True,
          "in_path": True},
         {"name": "appliance", "template": "vyos", "last_octet": 30, "unmanaged": True}]


def _targets():
    out = []
    for team, data in TEAMS.items():
        for i, b in enumerate(BOXES):
            tid = int(data["identifier"])
            out.append({"team_key": team, "box_name": b["name"], "vmid": 200 + tid * 10 + i,
                        "ip": f"192.168.{tid}.{b['last_octet']}"})
    return out


NODES = {"pve": {"pve_node": "proxmox", "endpoint": "https://10.0.0.150:8006",
                 "token_env": "T", "tls_fingerprint": "", "vmids": []}}
TOKENS = {"pve": {"user": "u@pve", "token_id": "u@pve!portal", "secret": "s3"}}


def _config(**kw):
    args = dict(comp_name="probe", run_id="run-abc", event_name="Probe",
                scoreboard_url="http://e", teams=TEAMS, boxes=BOXES, targets=_targets(),
                placement=None, default_node="pve", nodes=NODES, tokens=TOKENS)
    args.update(kw)
    return portal_ops.build_portal_config(**args)


class BuildConfig(unittest.TestCase):
    def test_boxes_per_team_with_os_and_firewall(self):
        cfg = _config()
        boxes = {b["name"]: b for b in cfg["teams"]["team1"]["boxes"]}
        self.assertEqual(sorted(boxes), ["dc01", "fw01", "web01"])  # appliance left out
        self.assertEqual(boxes["dc01"]["os"], "windows")
        self.assertEqual(boxes["web01"]["os"], "linux")
        self.assertTrue(boxes["fw01"]["firewall"])
        self.assertEqual(boxes["web01"]["vmid"], 1210)
        self.assertEqual(cfg["teams"]["team2"]["boxes"][0]["ip"], "192.168.102.10")
        self.assertEqual(cfg["nodes"]["pve"]["token"], "u@pve!portal=s3")

    def test_firewall_console_can_be_switched_off(self):
        names = [b["name"] for b in _config(firewall_console=False)["teams"]["team1"]["boxes"]]
        self.assertNotIn("fw01", names)

    def test_a_node_without_a_token_is_omitted(self):
        cfg = _config(tokens={})
        self.assertEqual(cfg["nodes"], {})
        self.assertEqual(len(cfg["teams"]["team1"]["boxes"]), 3)  # still listed

    def test_multi_node_box_records_carry_the_record_name(self):
        placement = {"team_nodes": {"team1": "a", "team2": "b"}, "nodes": {}}
        cfg = _config(placement=placement)
        self.assertEqual({b["node"] for b in cfg["teams"]["team2"]["boxes"]}, {"b"})


class _R:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class TunnelDecision(unittest.TestCase):
    def test_no_token_never_starts(self):
        self.assertEqual(portal_ops.tunnel_decision("h", "c", "r", "")[0], "skip")

    def test_token_without_hostname_skips(self):
        self.assertEqual(portal_ops.tunnel_decision("", "c", "r", "tok")[0], "skip")

    def test_unowned_hostname_starts(self):
        for resp in (_R(530, None), _R(200, None), _R(200, {"x": 1})):
            self.assertEqual(portal_ops.tunnel_decision("h", "c", "r", "tok",
                                                        fetch=lambda u, r=resp: r)[0], "start")

    def test_this_run_restarts_but_another_range_is_refused(self):
        mine = _R(200, {"comp": "c", "run_id": "r"})
        theirs = _R(200, {"comp": "other", "run_id": "r2"})
        self.assertEqual(portal_ops.tunnel_decision("h", "c", "r", "t", fetch=lambda u: mine)[0],
                         "start")
        verdict, why = portal_ops.tunnel_decision("h", "c", "r", "t", fetch=lambda u: theirs)
        self.assertEqual(verdict, "skip")
        self.assertIn("other", why)


class Bundle(unittest.TestCase):
    def test_bundle_ships_sources_and_secrets_0600(self):
        tar = portal_ops.bundle({"comp": "c"}, "PORTAL_SECRET_KEY=x\n")
        with tarfile.open(fileobj=io.BytesIO(tar)) as t:
            members = {m.name: m for m in t.getmembers()}
        for name in ("app.py", "auth.py", "console.py", "Dockerfile", "compose.yaml",
                     "frontend/package.json", "frontend/package-lock.json",
                     "frontend/src/pages/Console.jsx", "frontend/src/components/kit.jsx",
                     "portal.json", ".env"):
            self.assertIn(name, members)
        self.assertEqual(members[".env"].mode, 0o600)
        self.assertEqual(members["portal.json"].mode, 0o600)
        # Source only: the image runs `npm ci` + `vite build` itself.
        self.assertFalse(any("node_modules" in n or "/dist/" in n or "__pycache__" in n
                             for n in members))


class CheckArgs(unittest.TestCase):
    def test_one_sample_per_tokened_node(self):
        args = portal_ops.check_args("probe", TEAMS, "adminpw", _config())
        self.assertEqual([s["node"] for s in args["node_samples"]], ["pve"])
        self.assertEqual(args["teams"][0]["password"], "a")
        self.assertEqual(portal_ops.check_args("probe", TEAMS, "x",
                                               _config(tokens={}))["node_samples"], [])

    def test_probe_result_is_parsed_from_the_last_line(self):
        rows = portal_ops.check_portal(lambda cmd: (0, 'noise\n[{"check": "healthz", "ok": true,'
                                                       ' "detail": ""}]\n'),
                                       "probe", TEAMS, "pw", _config())
        self.assertEqual(rows[0]["check"], "healthz")
        with self.assertRaises(RuntimeError):
            portal_ops.check_portal(lambda cmd: (1, "Traceback"), "probe", TEAMS, "pw", _config())


class DeployHook(unittest.TestCase):
    """deploy_portal is opt-in and must never fail the deploy."""

    def _ctx(self, d, compfile):
        (d / "Compfile").write_text(compfile)
        saved = []

        class Ctx:
            comp_dir = d
            state = {}

            def save_state(self):
                saved.append(dict(self.state))
        return Ctx(), saved

    def test_off_without_the_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, saved = self._ctx(Path(tmp), "name probe\n")
            with patch.object(portal_ops, "_deploy_portal") as inner:
                portal_ops.deploy_portal(ctx)
        inner.assert_not_called()

    def test_a_failure_is_a_degradation_not_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, saved = self._ctx(Path(tmp), "portal 1\n")
            with patch.object(portal_ops, "_deploy_portal", side_effect=RuntimeError("boom")), \
                    patch.object(portal_ops, "timed",
                                 side_effect=lambda *a, **k: contextlib.nullcontext()), \
                    patch.object(portal_ops, "record_degradation") as degr, \
                    contextlib.redirect_stdout(io.StringIO()):
                portal_ops.deploy_portal(ctx)
        degr.assert_called_once()
        self.assertFalse(ctx.state["portal_up"])

    def test_phase8_calls_the_hook_last(self):
        sys.path.insert(0, str(ROOT / "tests"))
        from _deploy_patch import dpatch
        from deploy_lib.phases import seed as dl_seed

        order = []

        class Ctx:
            from_phase, comp_dir, scoring_ip = 1, Path("/nonexistent"), "10.0.0.9"
            teams, admin_password, injects = {}, "a", []
            state = {"seeded": True, "engine_unpaused": True}

            def save_state(self):
                pass

        with dpatch("wait_for_http"), dpatch("engine_paused", return_value=False), \
                dpatch("timed", side_effect=lambda *a, **k: contextlib.nullcontext()), \
                dpatch("deploy_portal", side_effect=lambda ctx: order.append("portal")), \
                contextlib.redirect_stdout(io.StringIO()):
            dl_seed.phase8_seed(Ctx())
        self.assertEqual(order, ["portal"])


if __name__ == "__main__":
    unittest.main()
