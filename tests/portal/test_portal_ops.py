"""Portal deploy side (portal_ops): portal.json, tunnel ownership, bundle, phase-8 hook."""

import base64
import contextlib
import io
import json
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from portal import portal_ops  # noqa: E402

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


NODES = {"pve": {"pve_node": "proxmox", "endpoint": "https://203.0.113.10:8006",
                 "token_env": "T", "tls_fingerprint": "", "vmids": []}}
TOKENS = {"pve": {"user": "u@pve", "token_id": "u@pve!portal", "secret": "s3"}}

PROBE_TEAMS = {"team1": {"identifier": "101", "password": "pw1"},
               "team2": {"identifier": "102", "password": "pw2"}}


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


    def test_a_node_without_a_token_is_omitted(self):
        cfg = _config(tokens={})
        self.assertEqual(cfg["nodes"], {})
        self.assertEqual(len(cfg["teams"]["team1"]["boxes"]), 3)  # still listed



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


def _probe_config(nodes=("pve-a", "pve-b"), public_host=""):
    return {"comp": "probe", "public_host": public_host, "nodes": {n: {} for n in nodes},
            "teams": {"team1": {"boxes": [{"name": "web01", "node": "pve-a"},
                                          {"name": "web02", "node": "pve-a"},
                                          {"name": "db01", "node": "pve-b"}]},
                      "team2": {"boxes": [{"name": "fw01", "node": "pve-a"}]}}}


class ProbeArgs(unittest.TestCase):
    def test_public_host_is_carried_only_when_the_tunnel_is_up(self):
        self.assertEqual(
            portal_ops.check_args("probe", PROBE_TEAMS, "admin",
                                  _probe_config(public_host="portal.example"))["public_host"],
            "portal.example")
        self.assertEqual(portal_ops.check_args("probe", PROBE_TEAMS, "admin",
                                               _probe_config())["public_host"], "")

    def test_every_node_contributes_a_sample(self):
        args = portal_ops.check_args("probe", PROBE_TEAMS, "admin", _probe_config())
        self.assertEqual({s["node"] for s in args["node_samples"]}, {"pve-a", "pve-b"})
        self.assertEqual(args["missing_nodes"], [])
        self.assertEqual(len(args["relay_targets"]), 2)  # default 1: per-node pass unchanged

    def test_a_node_without_a_box_is_reported_not_skipped(self):
        args = portal_ops.check_args("probe", PROBE_TEAMS, "admin",
                                     _probe_config(nodes=("pve-a", "pve-b", "pve-c")))
        self.assertEqual(args["missing_nodes"], ["pve-c"])

    def test_concurrency_adds_boxes_up_to_n(self):
        args = portal_ops.check_args("probe", PROBE_TEAMS, "admin", _probe_config(),
                                     concurrency=3)
        self.assertEqual(args["concurrency"], 3)
        self.assertEqual(len(args["relay_targets"]), 3)
        self.assertEqual({s["node"] for s in args["node_samples"]}, {"pve-a", "pve-b"})
        self.assertEqual(len({(t["team"], t["box"]) for t in args["relay_targets"]}), 3)


class CheckPortalArgs(unittest.TestCase):
    def _args_sent(self, config, **kw):
        seen = {}

        def run_engine(cmd):
            seen["cmd"] = cmd
            return 0, "[]"

        self.assertEqual(portal_ops.check_portal(run_engine, "probe", PROBE_TEAMS,
                                                 "admin", config, **kw), [])
        blob = re.search(r"tez-portal-check\.py (\S+);", seen["cmd"]).group(1)
        return json.loads(base64.b64decode(blob))

    def test_script_gets_public_host_and_concurrency(self):
        args = self._args_sent(_probe_config(public_host="portal.example"), concurrency=4)
        self.assertEqual((args["public_host"], args["concurrency"]), ("portal.example", 4))
        args = self._args_sent(_probe_config())
        self.assertEqual((args["public_host"], args["concurrency"]), ("", 1))

    def test_cli_exposes_the_concurrency_knob(self):
        from verifier.cli import build_parser

        self.assertEqual(build_parser().parse_args(["comp"]).portal_concurrency, 1)
        self.assertEqual(
            build_parser().parse_args(["comp", "--portal-concurrency", "8"]).portal_concurrency,
            8)

    def test_gate_forwards_concurrency_to_the_probe(self):
        from verifier import portal as v_portal

        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            (comp_dir / "Compfile").write_text("portal 1\n")
            (comp_dir / "portal.json").write_text("{}")
            seen = {}

            def fake_check(*a, **kw):
                seen.update(kw)
                return []

            with patch.object(v_portal, "portal_enabled", return_value=True), \
                    patch.object(v_portal, "check_portal", side_effect=fake_check):
                result = v_portal.check_portal_gate(None, comp_dir, {}, "pw", concurrency=7)
        self.assertEqual(seen["concurrency"], 7)
        self.assertTrue(result.passed)


class ProbeScript(unittest.TestCase):
    """Drive the generated engine probe against a refused loopback port (no live portal)."""

    def _refused_port(self):
        s = socket.socket()
        self.addCleanup(s.close)
        s.bind(("127.0.0.1", 0))  # bound, never listening: connect is refused immediately
        return s.getsockname()[1]

    def _args(self, **kw):
        args = {"comp": "probe", "port": self._refused_port(),
                "teams": [{"team": "team1", "password": "pw", "boxes": ["web01"]}],
                "admin_password": "admin", "public_host": "", "node_samples": [],
                "relay_targets": [], "missing_nodes": [], "concurrency": 1}
        args.update(kw)
        return args

    def _run(self, args):
        blob = base64.b64encode(json.dumps(args).encode()).decode()
        proc = subprocess.run([sys.executable, "-c", portal_ops._CHECK_SCRIPT, blob],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_no_tunnel_means_no_tunnel_rows(self):
        rows = self._run(self._args())
        self.assertIn("healthz", [r["check"] for r in rows])
        self.assertFalse([r for r in rows if r["check"].startswith("tunnel")])

    def test_a_broken_tunnel_is_fail_rows_not_a_crash(self):
        args = self._args(public_host=f"127.0.0.1:{self._refused_port()}",
                          relay_targets=[{"node": "pve-a", "team": "team1", "box": "web01"}])
        tunnel = [r for r in self._run(args) if r["check"].startswith("tunnel")]
        self.assertEqual([r["check"] for r in tunnel],
                         ["tunnel healthz", "tunnel team login", "tunnel console relay"])
        self.assertFalse(any(r["ok"] for r in tunnel))

    def test_a_node_without_a_sample_is_a_named_fail_row(self):
        rows = self._run(self._args(missing_nodes=["pve-c"]))
        row = next(r for r in rows if r["check"] == "console pve-c")
        self.assertFalse(row["ok"])
        self.assertIn("pve-c", row["detail"])


class Bundle(unittest.TestCase):
    def test_bundle_ships_sources_and_secrets_0600(self):
        tar = portal_ops.bundle({"comp": "c"}, "PORTAL_SECRET_KEY=x\n")
        with tarfile.open(fileobj=io.BytesIO(tar)) as t:
            members = {m.name: m for m in t.getmembers()}
        for name in ("app.py", "auth.py", "console.py", "ops_sync.py",
                     "Dockerfile", "compose.yaml",
                     "frontend/package.json", "frontend/package-lock.json",
                     "frontend/src/pages/Console.jsx", "frontend/src/components/kit.jsx",
                     "portal.json", ".env"):
            self.assertIn(name, members)
        self.assertEqual(members[".env"].mode, 0o600)
        self.assertEqual(members["portal.json"].mode, 0o600)
        # Source only: the image runs `npm ci` + `vite build` itself.
        self.assertFalse(any("node_modules" in n or "/dist/" in n or "__pycache__" in n
                             for n in members))


class EnvFileCookieSecure(unittest.TestCase):
    """`PORTAL_COOKIE_SECURE` must follow how the browser actually reaches the portal.

    Secure cookies are rejected over plain HTTP on any host that is not localhost (a secure
    context), so a hardcoded Secure made login impossible on the documented tunnel-less
    `ssh -L` path (caught live). Behind cloudflared the public side IS https, so
    the flag is still on there.
    """

    def test_follows_the_tunnel(self):
        with_tunnel = portal_ops._env_file("s", True, "tunnel-token", cookie_secure=True)
        no_tunnel = portal_ops._env_file("s", True, "", cookie_secure=False)
        self.assertIn("PORTAL_COOKIE_SECURE=1", with_tunnel)
        self.assertIn("TUNNEL_TOKEN=tunnel-token", with_tunnel)
        self.assertIn("PORTAL_COOKIE_SECURE=0", no_tunnel)
        self.assertNotIn("TUNNEL_TOKEN", no_tunnel)






class VpnEnrollment(unittest.TestCase):
    def _keys(self, d, entries):
        (d / "remote-access").mkdir()
        (d / "remote-access" / "keys.json").write_text(json.dumps(entries))


    def test_keys_json_maps_identifiers_onto_team_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._keys(d, [{"person": "alice", "team": "101", "key": "k1"},
                           {"person": "bob", "team": "102", "key": "k2"},
                           {"person": "gone", "team": "999", "key": "k3"}])
            env = {"TEZ_HEADSCALE_URL": "https://hs"}
            self.assertEqual(portal_ops.vpn_config(d, TEAMS, env), {
                "server_url": "https://hs",
                "teams": {"team1": [{"person": "alice", "key": "k1"}],
                          "team2": [{"person": "bob", "key": "k2"}]}})



# deploy_portal is opt-in and must never fail the deploy
class DeployHook(unittest.TestCase):
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
