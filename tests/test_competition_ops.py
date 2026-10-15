"""competition-ops.py: the post-deploy lifecycle driver.

Offline: the HTTP layer is a fake session; the filesystem is a tmpdir comp dir.
What is pinned: start flips the clock + engine and creates the DEFERRED injects as
the `scoring` account (never `admin` — one session per account); freeze/resume treat
the engine's stale-transition 400 as "already in state"; end records `ended` only on
a clean capture+pause; every action logs in as `scoring`.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import requests  # noqa: E402

from estate import state as estate_state  # noqa: E402

spec = importlib.util.spec_from_file_location("competition_ops", _REPO / "competition-ops.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)

BASE = "http://10.0.0.9"


class EngineBaseFromTheDocument(unittest.TestCase):
    """The engine IP comes from the state document unless --engine-ip overrides it."""

    def test_the_recorded_engine_ip_wins_over_the_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = Path(tmp)
            from estate import state as estate_state
            document = estate_state.EstateState.load(comp)
            document.absorb({"engine_mgmt_ip": "10.0.0.150", "run_id": "run-abc12345"})
            document.save()
            self.assertEqual(ops.engine_base(_Args("freeze", None, None), comp), "http://10.0.0.150")
            self.assertEqual(ops.engine_base(_Args("freeze", None, "10.9.9.9"), comp),
                             "http://10.9.9.9")

    def test_an_unrecorded_engine_refuses_and_names_the_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as raised:
                ops.engine_base(_Args("freeze", None, None), Path(tmp))
        self.assertIn("--engine-ip", str(raised.exception))


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, engine=None, post_status=None):
        self.engine = engine or {}
        self.post_status = post_status or {}
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        if url.endswith("/api/login"):
            return FakeResp(200, {"ok": True})
        if url.endswith("/api/engine/pause"):
            return FakeResp(self.post_status.get("pause", 200))
        return FakeResp(200, {"status": "success"})

    def get(self, url, timeout=None):
        self.calls.append(("GET", url))
        if url.endswith("/api/engine"):
            return FakeResp(200, self.engine)
        return FakeResp(200, [])


def _comp_dir(d, state=None, injects=None):
    comp = Path(d)
    estate_state.write_flat(comp, state or {"scoring_password": "sp", "teams": {}})
    for i, title in enumerate(injects or []):
        sub = comp / "injects" / f"i{i}"
        sub.mkdir(parents=True)
        (sub / "inject.json").write_text(json.dumps(
            {"title": title, "open_offset_min": 5, "due_offset_min": 60,
             "close_offset_min": 90}))
    return comp


def _args(comp, action, engine_ip=BASE):
    return _Args(action, str(comp), engine_ip)


class _Args:
    def __init__(self, action, comp_dir, engine_ip):
        self.action, self.comp_dir, self.engine_ip = action, comp_dir, engine_ip
        self.scoring_password = None


def _patch_session(fake):
    return unittest.mock.patch.object(ops.requests, "Session", lambda: fake)


class Start(unittest.TestCase):
    def test_held_start_flips_clock_engine_and_creates_deferred_injects(self):
        fake = FakeSession(engine={"competition_started": False, "running": False})
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d, injects=["Inject one"])
            with _patch_session(fake), \
                    unittest.mock.patch.object(ops, "create_injects",
                                               return_value=(1, [])) as p_create:
                rc = ops.cmd_start(_args(comp, "start"), comp)
            self.assertEqual(rc, 0)
            posted = [c for c in fake.calls if c[0] == "POST"]
            self.assertIn(("POST", f"{BASE}/api/competition/start", {"started": True}), posted)
            self.assertIn(("POST", f"{BASE}/api/engine/pause", {"pause": False}), posted)
            p_create.assert_called_once()
            self.assertEqual(p_create.call_args.args[0], BASE)
            self.assertEqual(p_create.call_args.kwargs.get("username"), "scoring")
            state = estate_state.load_flat(comp)
        self.assertTrue(state["started"])
        self.assertTrue(state["engine_unpaused"])
        self.assertTrue(state["injects_created"])

    def test_already_running_start_is_idempotent(self):
        state0 = {"scoring_password": "sp", "injects_created": True,
                  "injects_fingerprint": "x"}
        fake = FakeSession(engine={"competition_started": True, "running": True})
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d, state=dict(state0))
            with _patch_session(fake), \
                    unittest.mock.patch.object(ops, "create_injects") as p_create:
                rc = ops.cmd_start(_args(comp, "start"), comp)
            self.assertEqual(rc, 0)
            self.assertEqual([c for c in fake.calls if c[0] == "POST" and "login" not in c[1]], [])
            p_create.assert_not_called()
            state = estate_state.load_flat(comp)
        self.assertTrue(state["started"])

    def test_engine_unpaused_concurrently_is_not_a_failure(self):
        # The engine 400s the stale pause POST, then the re-GET proves it is running.
        fake = FakeSession(engine={"competition_started": True, "running": False})
        fake.post_status["pause"] = 400
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            with _patch_session(fake), \
                    unittest.mock.patch.object(
                        ops, "engine_status",
                        side_effect=[{"competition_started": True, "running": False},
                                     {"competition_started": True, "running": True}]):
                rc = ops.cmd_start(_args(comp, "start"), comp)
        self.assertEqual(rc, 0)


class FreezeResume(unittest.TestCase):
    def test_freeze_tolerates_the_stale_transition_400(self):
        fake = FakeSession(engine={"running": False})
        fake.post_status["pause"] = 400
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            with _patch_session(fake):
                rc = ops.cmd_freeze(_args(comp, "freeze"), comp, pause=True)
        self.assertEqual(rc, 0)

    def test_resume_reports_a_real_400_as_failure(self):
    # A 403 with the engine NOT already in the requested state is a real failure,
    #         not a stale transition.
        fake = FakeSession(engine={"running": False})
        fake.post_status["pause"] = 403
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            with _patch_session(fake):
                with self.assertRaises(requests.HTTPError):
                    ops.cmd_freeze(_args(comp, "resume"), comp, pause=False)


class End(unittest.TestCase):
    def _run(self, rc_of_end):
        fake = FakeSession()
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            with _patch_session(fake), \
                    unittest.mock.patch.object(ops, "end_event", return_value=rc_of_end):
                rc = ops.cmd_end(_args(comp, "end"), comp)
            state = estate_state.load_flat(comp)
        return rc, state

    def test_a_clean_end_records_ended(self):
        rc, state = self._run(0)
        self.assertEqual(rc, 0)
        self.assertTrue(state["ended"])

    def test_a_failed_end_never_claims_the_event_is_over(self):
        rc, state = self._run(1)
        self.assertEqual(rc, 1)
        self.assertNotIn("ended", state)


if __name__ == "__main__":
    unittest.main()
