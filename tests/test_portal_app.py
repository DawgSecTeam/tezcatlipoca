"""Portal HTTP surface (portal/app.py): login, team scoping, the gate, admin-only routes.

The isolation boundary lives here — the console token can open any team VM — so the
cross-team refusals are the tests that matter most."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import toml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from portal.app import LoginLimiter, create_app  # noqa: E402

CONFIG = {
    "comp": "probe", "run_id": "run-abc", "event_name": "Probe Event",
    "scoreboard_url": "http://10.0.0.252",
    "nodes": {"pve": {"endpoint": "https://10.0.0.150:8006", "pve_node": "proxmox",
                      "token": "u@pve!portal=s", "tls_fingerprint": ""}},
    "teams": {
        "team1": {"identifier": "101", "boxes": [
            {"name": "web01", "os": "linux", "ip": "192.168.101.10", "vmid": 1210,
             "node": "pve", "firewall": False},
            {"name": "dc01", "os": "windows", "ip": "192.168.101.20", "vmid": 1211,
             "node": "pve", "firewall": False}]},
        "team2": {"identifier": "102", "boxes": [
            {"name": "web01", "os": "linux", "ip": "192.168.102.10", "vmid": 1220,
             "node": "pve", "firewall": False}]},
    },
}
EVENT_CONF = {"admin": [{"name": "admin", "pw": "adminpw"}, {"name": "scoring", "pw": "s"}],
              "team": [{"name": "team1", "pw": "t1pw"}, {"name": "team2", "pw": "t2pw"}]}


class FakeBroker:
    def __init__(self, configured=True):
        self.configured = configured
        self.minted = []

    def available(self, box=None):
        return self.configured

    async def mint(self, box, owner):
        self.minted.append((box["vmid"], owner))
        return {"relay_id": f"rid-{box['vmid']}", "password": "ticket"}

    def take(self, relay_id, owner):
        return None

    async def relay(self, client, entry):  # pragma: no cover - take() always refuses here
        raise AssertionError("relay must not run")


class PortalApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "portal.json").write_text(json.dumps(CONFIG))
        (d / "event.conf").write_text(toml.dumps(EVENT_CONF))
        self.state = d / "state"
        self.broker = FakeBroker()
        self.make()

    def make(self, **kw):
        d = Path(self.tmp.name)
        kw.setdefault("broker", self.broker)
        kw.setdefault("dist_dir", None)
        self.app = create_app(d / "portal.json", d / "event.conf", self.state, "k" * 32,
                              cookie_secure=False, **kw)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def login(self, user, pw, client=None):
        return (client or self.client).post("/api/login",
                                            json={"username": user, "password": pw})

    def open_gate(self):
        admin = TestClient(self.app)
        self.assertEqual(self.login("admin", "adminpw", admin).status_code, 200)
        self.assertEqual(admin.post("/api/admin/access", json={"open": True}).status_code, 200)

    # -- login ---------------------------------------------------------------------------

    def test_unauthenticated_requests_get_401(self):
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.assertEqual(self.client.post("/api/console", json={"box": "web01"}).status_code, 401)
        self.assertEqual(self.client.get("/api/admin/teams").status_code, 401)

    def test_wrong_password_and_refused_accounts(self):
        self.assertEqual(self.login("team1", "nope").status_code, 401)
        self.assertEqual(self.login("scoring", "s").status_code, 401)

    def test_logout_ends_the_session(self):
        self.login("team1", "t1pw")
        self.assertEqual(self.client.get("/api/me").status_code, 200)
        self.client.post("/api/logout", json={})
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_failed_logins_are_rate_limited_per_client(self):
        self.make(limiter=LoginLimiter(max_fails=3))
        for _ in range(3):
            self.assertEqual(self.login("team1", "bad").status_code, 401)
        self.assertEqual(self.login("team1", "t1pw").status_code, 429)

    def test_cross_origin_posts_are_refused(self):
        r = self.client.post("/api/login", json={"username": "team1", "password": "t1pw"},
                             headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    # -- team scoping (the isolation boundary) ------------------------------------------

    def test_team_sees_only_its_own_boxes_and_no_vmids(self):
        self.login("team1", "t1pw")
        me = self.client.get("/api/me").json()
        self.assertEqual(me["team"], "team1")
        self.assertEqual([b["name"] for b in me["boxes"]], ["web01", "dc01"])
        self.assertNotIn("teams", me)
        self.assertNotIn("vmid", json.dumps(me))
        self.assertNotIn("u@pve", json.dumps(me))

    def test_team_cannot_open_another_teams_box(self):
        self.open_gate()
        self.login("team1", "t1pw")
        r = self.client.post("/api/console", json={"team": "team2", "box": "web01"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.broker.minted, [])

    def test_a_box_name_resolves_inside_the_session_team_only(self):
        """team2's web01 shares team1's box NAME: a team session always gets its own."""
        self.open_gate()
        self.login("team1", "t1pw")
        r = self.client.post("/api/console", json={"box": "web01"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.broker.minted[0][0], 1210)

    def test_unknown_box_gives_the_same_answer_as_a_foreign_one(self):
        self.open_gate()
        self.login("team2", "t2pw")
        r = self.client.post("/api/console", json={"box": "dc01"})  # exists only on team1
        self.assertEqual(r.status_code, 403)

    def test_admin_only_routes(self):
        self.login("team1", "t1pw")
        self.assertEqual(self.client.get("/api/admin/teams").status_code, 403)
        self.assertEqual(self.client.post("/api/admin/access", json={"open": True}).status_code,
                         403)

    # -- the gate -----------------------------------------------------------------------

    def test_gate_closed_by_default_returns_423_for_teams(self):
        self.login("team1", "t1pw")
        self.assertEqual(self.client.post("/api/console", json={"box": "web01"}).status_code,
                         423)
        self.assertFalse(self.client.get("/api/me").json()["access"]["open"])

    def test_admin_bypasses_the_gate_and_sees_every_team(self):
        self.login("admin", "adminpw")
        me = self.client.get("/api/me").json()
        self.assertEqual(sorted(me["teams"]), ["team1", "team2"])
        r = self.client.post("/api/console", json={"team": "team2", "box": "web01"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ws_path"], "/console/ws/rid-1220")

    def test_opening_the_gate_lets_teams_in_and_is_logged(self):
        self.open_gate()
        self.login("team1", "t1pw")
        self.assertEqual(self.client.post("/api/console", json={"box": "dc01"}).status_code, 200)
        log = (self.state / "access.log").read_text()
        self.assertIn('"event": "gate"', log)
        self.assertIn('"event": "console_open"', log)

    def test_default_open_starts_with_the_gate_open(self):
        self.make(default_open=True)
        self.login("team1", "t1pw")
        self.assertEqual(self.client.post("/api/console", json={"box": "web01"}).status_code, 200)

    def test_consoles_unconfigured_returns_503(self):
        self.make(broker=FakeBroker(configured=False), default_open=True)
        self.login("team1", "t1pw")
        self.assertEqual(self.client.post("/api/console", json={"box": "web01"}).status_code, 503)

    # -- relay endpoint -----------------------------------------------------------------

    def test_websocket_without_a_valid_relay_id_is_refused(self):
        from starlette.websockets import WebSocketDisconnect

        self.login("team1", "t1pw")
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/console/ws/forged") as ws:
                ws.receive_bytes()

    def test_errors_carry_detail_like_webui(self):
        r = self.login("team1", "nope")
        self.assertIn("detail", r.json())

    def test_spa_fallback_never_shadows_the_api(self):
        dist = Path(self.tmp.name) / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<div id=root></div>")
        (dist / "assets" / "a.js").write_text("x")
        self.make(dist_dir=dist)
        for path in ("/", "/console/team1/web01"):
            r = self.client.get(path)
            self.assertEqual((r.status_code, r.text), (200, "<div id=root></div>"), path)
        self.assertEqual(self.client.get("/assets/a.js").text, "x")
        self.assertEqual(self.client.get("/api/me").status_code, 401)       # API, not the SPA
        self.assertEqual(self.client.get("/api/nope").status_code, 404)
        self.assertEqual(self.client.get("/healthz").json()["comp"], "probe")

    def test_healthz_names_the_run(self):
        hz = self.client.get("/healthz").json()
        self.assertEqual((hz["comp"], hz["run_id"], hz["open"]), ("probe", "run-abc", False))


if __name__ == "__main__":
    unittest.main()
