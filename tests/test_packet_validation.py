"""Compile-boundary validation: the packet shapes that used to survive
validate_profile and die later in a live deploy (D3), some only at phase 6 after DC
promotion. Offline — validate_profile is pure."""

import copy
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from packet_ops import validate_profile

# A minimal profile that validates cleanly (checked by test_base_profile_is_valid);
# each test deep-copies it and introduces exactly one defect.
_BASE = {
    "event": {"comp_id": "pkt-val", "name": "Validation Test",
              "scenario": "scenario line", "difficulty": 3},
    "domain": {"name_template": "mira-{team}.corp.sus"},
    "credentials": {
        "box_username": "blueteam",
        "box_password": "n0t_sus1",
        "credlists": {"linux": {"blueteam": "n0t_sus1"}},
        "domain_accounts": [{"username": "Red", "password": "Red123!", "admin": True,
                             "full_name": "Red Team"}],
        "out_of_scope": ["scorebot", "blackteam"],
    },
    "boxes": [
        {"name": "ad01", "template": "base-windows-server", "last_octet": 2,
         "domain_role": "dc", "fidelity": "substituted"},
        {"name": "web01", "template": "base-fedora44-fix", "last_octet": 4,
         "fidelity": "exact"},
    ],
    "services": [
        {"box": "web01", "name": "Web", "port": 80, "pin": "apache",
         "display": "http", "fidelity": "exact"},
    ],
    "injects": [{"slug": "01-welcome", "title": "Welcome", "open_offset_min": 0,
                 "due_offset_min": 30, "close_offset_min": 60}],
}


def _mutated(**changes):
    """Deep-copied base profile with top-level keys replaced."""
    p = copy.deepcopy(_BASE)
    p.update(copy.deepcopy(changes))
    return p


class ValidationBase(unittest.TestCase):
    def test_base_profile_is_valid(self):
        self.assertEqual(validate_profile(copy.deepcopy(_BASE)), [])


class DomainAccounts(unittest.TestCase):
    """domain_ops adds these at phase 6 (acct["username"] / acct["password"]) — a
    malformed entry was a KeyError/TypeError deep in a live deploy."""

    def test_missing_password_rejected(self):
        errs = validate_profile(_mutated(credentials={
            **_BASE["credentials"],
            "domain_accounts": [{"username": "Red"}]}))
        self.assertTrue(any("domain_accounts[0].password is required" in e
                            for e in errs), errs)

    def test_non_mapping_entry_rejected(self):
        errs = validate_profile(_mutated(credentials={
            **_BASE["credentials"],
            "domain_accounts": ["Red"]}))
        self.assertTrue(any("domain_accounts[0] must be a mapping" in e
                            for e in errs), errs)

    def test_not_a_list_rejected(self):
        errs = validate_profile(_mutated(credentials={
            **_BASE["credentials"],
            "domain_accounts": {"username": "Red", "password": "x"}}))
        self.assertTrue(any("domain_accounts must be a list" in e for e in errs), errs)

    def test_mixed_case_ad_name_allowed(self):
        # CDE ships Red/Blue/Green — AD names are not unix usernames.
        self.assertEqual(validate_profile(copy.deepcopy(_BASE)), [])


class BoxPassword(unittest.TestCase):
    """The Windows 8-char floor was enforced only for baseline credlist users
    (build_baseline), never for box_password, which bootstrap pushes with net user /
    the guest agent."""

    def test_short_password_on_windows_box_rejected(self):
        errs = validate_profile(_mutated(credentials={
            **_BASE["credentials"], "box_password": "short"}))
        self.assertTrue(any("box_password is 5 chars but Windows box(es) ['ad01']" in e
                            for e in errs), errs)

    def test_eight_chars_passes(self):
        self.assertEqual(validate_profile(_mutated(credentials={
            **_BASE["credentials"], "box_password": "12345678"})), [])

    def test_short_password_ok_without_windows_boxes(self):
        # Linux has no such floor; a Linux-only lineup keeps its packet password.
        p = _mutated(credentials={**_BASE["credentials"], "box_password": "abc"},
                     boxes=[b for b in _BASE["boxes"] if b["name"] != "ad01"])
        self.assertEqual(validate_profile(p), [])


class OutOfScopeUsernames(unittest.TestCase):
    """These become local-user USERNAME vars on every managed box, but got none of
    the credlist name rules."""

    def test_invalid_username_rejected(self):
        errs = validate_profile(_mutated(credentials={
            **_BASE["credentials"], "out_of_scope": ["Score Bot"]}))
        self.assertTrue(any("out_of_scope: username 'Score Bot'" in e for e in errs), errs)

    def test_legacy_account_name_rejected(self):
        errs = validate_profile(_mutated(credentials={
            **_BASE["credentials"], "out_of_scope": ["daemon"]}))
        self.assertTrue(any("out_of_scope: 'daemon' collides" in e for e in errs), errs)

    def test_not_a_list_rejected(self):
        errs = validate_profile(_mutated(credentials={
            **_BASE["credentials"], "out_of_scope": "scorebot"}))
        self.assertTrue(any("out_of_scope must be a list" in e for e in errs), errs)


class CompfileInjection(unittest.TestCase):
    """The Compfile is line-oriented (utils.load_compfile splits `key value` per
    line): a newline in an interpolated field truncates it or injects keys."""

    def test_event_name_newline_rejected(self):
        errs = validate_profile(_mutated(event={
            **_BASE["event"], "name": "Real Name\ndifficulty 10"}))
        self.assertTrue(any("event.name" in e and "control character" in e
                            for e in errs), errs)

    def test_event_scenario_newline_rejected(self):
        errs = validate_profile(_mutated(event={
            **_BASE["event"], "scenario": "line one\nname pwned"}))
        self.assertTrue(any("event.scenario" in e and "control character" in e
                            for e in errs), errs)

    def test_domain_name_template_newline_rejected(self):
        errs = validate_profile(_mutated(domain={
            "name_template": "mira-{team}.corp.sus\nname pwned"}))
        self.assertTrue(any("domain.name_template" in e and "control character" in e
                            for e in errs), errs)


class ServiceFidelity(unittest.TestCase):
    """render_fidelity defaulted a missing services[].fidelity to "exact", claiming
    parity the packet may not have (boxes[].fidelity was already required)."""

    def test_missing_fidelity_rejected(self):
        svc = {k: v for k, v in _BASE["services"][0].items() if k != "fidelity"}
        errs = validate_profile(_mutated(services=[svc]))
        self.assertTrue(any("fidelity must be one of" in e for e in errs), errs)

    def test_unknown_fidelity_rejected(self):
        errs = validate_profile(_mutated(services=[
            {**_BASE["services"][0], "fidelity": "banana"}]))
        self.assertTrue(any("fidelity must be one of" in e for e in errs), errs)


class ServiceRequiredVars(unittest.TestCase):
    """REQUIRED_VARS literal vars were only checked at deploy start
    (nakon_ops._validate_pin_vars), after the golden bundle was already built."""

    def test_missing_literal_var_rejected(self):
        svc = {"box": "web01", "name": "hosts redirect", "port": 80,
               "pin": "hosts-redirect-linux", "scored": False, "fidelity": "exact"}
        errs = validate_profile(_mutated(services=[svc]))
        self.assertTrue(any("hosts-redirect-linux" in e and "'HOSTS'" in e
                            for e in errs), errs)

    def test_literal_var_supplied_passes(self):
        svc = {"box": "web01", "name": "hosts redirect", "port": 80,
               "pin": "hosts-redirect-linux", "scored": False, "fidelity": "exact",
               "vars": {"HOSTS": "web01.example"}}
        self.assertEqual(validate_profile(_mutated(services=[svc])), [])

    def test_identity_var_not_required_at_compile(self):
        # "ip"/"ip:<box>" kinds are auto-filled per machine at stage generation, so a
        # pin without them must not be rejected here.
        svc = {"box": "web01", "name": "airship app", "port": 80,
               "pin": "airship-webapp", "scored": False, "fidelity": "exact",
               "vars": {"DB_USER": "a", "DB_PASS": "b"}}
        self.assertEqual(validate_profile(_mutated(services=[svc])), [])

    def test_vars_must_be_a_mapping(self):
        errs = validate_profile(_mutated(services=[
            {**_BASE["services"][0], "vars": "HOSTS=x"}]))
        self.assertTrue(any("vars must be a mapping" in e for e in errs), errs)


if __name__ == "__main__":
    unittest.main()
