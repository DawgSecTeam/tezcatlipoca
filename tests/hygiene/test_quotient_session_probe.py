"""tools/quotient-session-probe.py: measure (not assume) Quotient session eviction.

Offline: the HTTP layer is a fake requests module and the comp dir is a tmpdir state
document. The fake models a server that evicts only the same account's previous cookie
so the cross-account rows prove the probe reads per-account behavior. A rejected login
is a setup failure (exit 2), not an eviction measurement.
"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from estate import state as estate_state  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "quotient_session_probe", _REPO / "tools" / "quotient-session-probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

BASE = "http://10.0.0.92"
TEAMS = {
    "team1": {"identifier": "101", "password": "team1-pw"},
    "team2": {"identifier": "102", "password": "team2-pw"},
}


class _Cookies:
    def __init__(self, values):
        self._values = dict(values)

    def get_dict(self):
        return dict(self._values)


class _Resp:
    def __init__(self, status=200, cookies=None):
        self.status_code = status
        self.cookies = _Cookies(cookies or {})


class _FakeQuotient:
    def __init__(self, evicts=False, login_status=None):
        self.evicts = evicts
        self.login_status = dict(login_status or {})
        self.valid = {}
        self.logins = {}
        self.calls = []

    def post(self, url, json=None, timeout=None):
        username = json["username"]
        self.calls.append(("POST", url))
        status = self.login_status.get(username, 200)
        if status >= 400:
            return _Resp(status)
        count = self.logins.get(username, 0) + 1
        self.logins[username] = count
        token = f"{username}-{count}"
        if self.evicts:
            self.valid = {cookie: user for cookie, user in self.valid.items()
                          if user != username}
        self.valid[token] = username
        return _Resp(status, {"session": token})

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url))
        cookie = (headers or {}).get("Cookie", "")
        token = cookie.split("=", 1)[1] if "=" in cookie else ""
        return _Resp(200 if token in self.valid else 403)


def _comp_dir(d, state=None, credentials=None):
    comp = Path(d)
    estate_state.write_flat(comp, state or {
        "teams": TEAMS,
        "admin_password": "admin-pw",
        "scoring_password": "scoring-pw",
    })
    (comp / "teams.json").write_text(json.dumps(TEAMS))
    if credentials is not None:
        (comp / "credentials.txt").write_text(credentials)
    return comp


def _run(fake, comp=None, manual=None):
    argv = ["--base-url", BASE, "--json"]
    if comp is not None:
        argv.extend(["--comp-dir", str(comp)])
    else:
        argv.extend(["--username", manual[0], "--password", manual[1]])
    out = io.StringIO()
    with patch.object(probe, "requests", fake), contextlib.redirect_stdout(out):
        rc = probe.main(argv)
    return rc, json.loads(out.getvalue()), out.getvalue()


def _row(result, label):
    return next(row for row in result["rows"] if row["label"] == label)


class EvictionObserved(unittest.TestCase):
    def test_same_account_second_login_kills_the_first_cookie(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            fake = _FakeQuotient(evicts=True)
            rc, result, text = _run(fake, comp=comp)
        row = _row(result, "team1 twice")
        self.assertEqual(row["first_login_status"], 200)
        self.assertEqual(row["second_login_status"], 200)
        self.assertEqual(row["first_cookie_after"], 403)
        self.assertEqual(row["second_cookie_after"], 200)
        self.assertTrue(row["evicted"])
        self.assertEqual(result["summary"]["result"], "eviction-observed")
        self.assertEqual(rc, 0, "an observed eviction is a measurement, not a failure")
        for secret in ("team1-pw", "admin-pw", "scoring-pw"):
            self.assertNotIn(secret, text)
        self.assertEqual({url for method, url in fake.calls if method == "POST"},
                         {f"{BASE}/api/login"})
        self.assertEqual({url for method, url in fake.calls if method == "GET"},
                         {f"{BASE}/api/teams"})


class NoEviction(unittest.TestCase):
    def test_same_account_keeps_both_cookies(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            rc, result, _ = _run(_FakeQuotient(evicts=False), comp=comp)
        row = _row(result, "team1 twice")
        self.assertEqual(row["first_cookie_after"], 200)
        self.assertEqual(row["second_cookie_after"], 200)
        self.assertFalse(row["evicted"])
        self.assertEqual(result["summary"]["result"], "no-eviction")
        self.assertEqual(rc, 0)


class CrossAccount(unittest.TestCase):
    def test_another_accounts_login_leaves_the_first_cookie_valid(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            _, result, _ = _run(_FakeQuotient(evicts=True), comp=comp)
        for label in ("admin then scoring", "team1 then team2"):
            row = _row(result, label)
            self.assertEqual(row["first_cookie_after"], 200, label)
            self.assertEqual(row["second_cookie_after"], 200, label)
            self.assertFalse(row["evicted"], label)
        self.assertTrue(_row(result, "team1 twice")["evicted"],
                        "the fake must still evict within one account")


class LoginFailure(unittest.TestCase):
    def test_a_rejected_login_is_a_setup_failure_not_an_eviction(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            rc, result, _ = _run(_FakeQuotient(evicts=True,
                                               login_status={"team1": 401}), comp=comp)
        row = _row(result, "team1 twice")
        self.assertEqual(row["first_login_status"], 401)
        self.assertIsNone(row["first_cookie_after"])
        self.assertFalse(row["evicted"])
        self.assertEqual(result["summary"]["result"], "login-failure")
        self.assertIn("team1 twice", result["summary"]["login_failures"])
        self.assertEqual(rc, 2)


class CompDirAccounts(unittest.TestCase):
    def test_credentials_txt_supplies_admin_and_scoring_when_state_omits_them(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(
                d,
                state={"teams": TEAMS},
                credentials=("# Credentials for probe\n"
                             "admin  admin-pw\n"
                             "scoring  scoring-pw\n"))
            rc, result, _ = _run(_FakeQuotient(evicts=False), comp=comp)
        self.assertEqual([row["label"] for row in result["rows"]],
                         ["team1 twice", "admin twice", "admin then scoring",
                          "team1 then team2"])
        self.assertEqual(rc, 0)


class TeamsJsonFallback(unittest.TestCase):
    def test_teams_json_supplies_team_passwords_when_state_has_no_teams(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d, state={"admin_password": "admin-pw",
                                      "scoring_password": "scoring-pw"})
            rc, result, _ = _run(_FakeQuotient(evicts=False), comp=comp)
        self.assertEqual([row["label"] for row in result["rows"]],
                         ["team1 twice", "admin twice", "admin then scoring",
                          "team1 then team2"])
        self.assertEqual(rc, 0)


class ManualAccount(unittest.TestCase):
    def test_username_password_runs_only_the_same_account_row(self):
        rc, result, text = _run(_FakeQuotient(evicts=False),
                                manual=("operator", "operator-pw"))
        self.assertEqual([row["label"] for row in result["rows"]], ["operator twice"])
        self.assertEqual(rc, 0)
        self.assertNotIn("operator-pw", text)
        self.assertIn("--password '<password>'", result["rerun"][0])


if __name__ == "__main__":
    unittest.main()
