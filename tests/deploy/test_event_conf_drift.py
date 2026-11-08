"""A1 — a resumed deploy must not keep scoring a pre-fix pin set.

run-14ac7f14: attempt-1 baked event.conf at phase 3, the box_services fix (IIS→SMB)
landed at the failure boundary, phases 4–8 resumed — and run-bd70a985 started every
team with a phantom win01-iis pin while win01-smb never scored. ensure_event_conf_current
compares parsed TOML and re-pushes on drift; format-only differences are not drift."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

import toml  # noqa: E402
from engine import engine_event_ops  # noqa: E402

TEAMS = {"team1": {"identifier": "130", "password": "pw1!"}}
BOXES = [{"name": "win01", "last_octet": 3}]
CTX = {"ssh_key_path": "/k", "scoring_engine_ip": "10.0.0.1", "vm_username": "ops"}
SERVICES = {"win01": ["New SMB Share"]}


def _desired():
    conf = engine_event_ops.build_event_conf(
        engine_event_ops._quotient_ctx(TEAMS, BOXES, "c1", "adminpw", None, None),
        SERVICES)
    return toml.dumps(conf)


def _run(calls, live_toml, fail_read=False):
    def fake(ctx, cmd, step, check=True, timeout=60, capture=False, input=None):
        calls.append((step, cmd))
        if "cat" in cmd:
            if fail_read:
                raise RuntimeError("ssh refused")
            return type("R", (), {"stdout": live_toml})()
        return type("R", (), {"stdout": ""})()
    return fake


def _drift(live_toml, fail_read=False):
    calls = []
    with mock.patch.object(engine_event_ops, "_run_engine_cmd", _run(calls, live_toml, fail_read)):
        with tempfile.TemporaryDirectory() as td:
            comp = Path(td)
            (comp / "box_services.json").write_text(json.dumps(SERVICES))
            out = engine_event_ops.ensure_event_conf_current(
                comp, TEAMS, BOXES, CTX, "c1", admin_password="adminpw",
                box_creds={"analyst": "pw"})
    return out, calls


class DriftGuard(unittest.TestCase):
    def test_identical_conf_is_current_and_touches_nothing(self):
        out, calls = _drift(_desired())
        self.assertIs(out, True)
        self.assertEqual([s for s, _ in calls], ["read event.conf"])

    def test_reformatted_but_equal_is_not_drift(self):
        same_but_differently_spaced = _desired().replace("Port = 445", "Port  =  445")
        self.assertNotEqual(same_but_differently_spaced, _desired())
        out, calls = _drift(same_but_differently_spaced)
        self.assertIs(out, True)
        self.assertEqual(len(calls), 1)

    def test_pin_drift_repushes_and_restarts(self):
        stale = json.loads(json.dumps(  # old pin set: win01 scored on IIS, not SMB
            {"RequiredSettings": {"EventName": "c1", "EventType": "rvb",
                                  "BindAddress": "0.0.0.0"},
             "MiscSettings": {"StartPaused": True, "Delay": 60, "Jitter": 10, "Points": 5},
             "admin": [{"name": "admin", "pw": "adminpw"}],
             "team": [{"name": "team1", "pw": "pw1!"}],
             "box": [{"name": "win01", "ip": "192.168._.3",
                      "Web": [{"Display": "iis", "Port": 80, "Scheme": "http",
                               "Url": [{"Path": "/", "Status": 200}]}]}]}))
        out, calls = _drift(toml.dumps(stale))
        self.assertIs(out, False)
        steps = [s for s, _ in calls]
        self.assertIn("push event.conf", steps)
        self.assertIn("restart quotient server", steps)
        self.assertNotIn("push quotient .env", steps)   # never rewrite db secrets on drift
        pushed = [c for s, c in calls if s == "push event.conf"][0]
        import base64
        b64 = pushed.split("echo '")[1].split("'")[0]
        conf = toml.loads(base64.b64decode(b64).decode())
        self.assertIn("Tcp", conf["box"][0])            # SMB pin present
        self.assertNotIn("Web", conf["box"][0])         # phantom IIS gone

    def test_unreadable_engine_yields_undecidable_not_push(self):
        out, calls = _drift("", fail_read=True)
        self.assertIsNone(out)
        self.assertEqual([s for s, _ in calls], ["read event.conf"])


if __name__ == "__main__":
    unittest.main()
