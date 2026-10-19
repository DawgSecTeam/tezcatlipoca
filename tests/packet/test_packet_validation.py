"""Compile-boundary validation: packet shapes that must die in validate_profile, not in a live deploy."""

import copy
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from config.packet_ops import validate_profile

# Minimal cleanly-valid profile; each test deep-copies it and injects exactly one defect.
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
    p = copy.deepcopy(_BASE)
    p.update(copy.deepcopy(changes))
    return p




# domain_ops reads acct["username"]/acct["password"] at phase 6; malformed entries died there.
class DomainAccounts(unittest.TestCase):
    def test_missing_password_rejected(self):
        errs = validate_profile(_mutated(credentials={
            **_BASE["credentials"],
            "domain_accounts": [{"username": "Red"}]}))
        self.assertTrue(any("domain_accounts[0].password is required" in e
                            for e in errs), errs)


# The Windows 8-char floor must cover box_password (pushed via net user / guest agent), not just credlist users.
class BoxPassword(unittest.TestCase):
    def test_short_password_on_windows_box_rejected(self):
        errs = validate_profile(_mutated(credentials={
            **_BASE["credentials"], "box_password": "short"}))
        self.assertTrue(any("box_password is 5 chars but Windows box(es) ['ad01']" in e
                            for e in errs), errs)




# Credlist entries become local-user accounts on managed boxes and must pass the same policies.
class WindowsCredlistPasswords(unittest.TestCase):
    def _profile(self, password):
        p = copy.deepcopy(_BASE)
        p["credentials"]["credlists"]["linux"] = {"svc_imaging": password}
        p["services"] = [{"box": "ad01", "name": "FTP", "port": 21,
                          "pin": "IIS FTP", "display": "ftp", "fidelity": "exact"}]
        return p

    def test_username_in_password_rejected(self):
        errs = validate_profile(self._profile("Svc_imaging9!"))
        self.assertTrue(any("must not contain the username" in e for e in errs), errs)

    def test_fullname_policy_is_checked(self):
        errs = validate_profile(self._profile("Service9!"))
        self.assertTrue(any("full name" in e for e in errs), errs)



class OutOfScopeUsernames(unittest.TestCase):
    def test_invalid_username_rejected(self):
        errs = validate_profile(_mutated(credentials={
            **_BASE["credentials"], "out_of_scope": ["Score Bot"]}))
        self.assertTrue(any("out_of_scope: username 'Score Bot'" in e for e in errs), errs)

    def test_legacy_account_name_rejected(self):
        errs = validate_profile(_mutated(credentials={
            **_BASE["credentials"], "out_of_scope": ["daemon"]}))
        self.assertTrue(any("out_of_scope: 'daemon' collides" in e for e in errs), errs)



# Compfile is line-oriented (`key value` per line): a newline in a field truncates it or injects keys.
class CompfileInjection(unittest.TestCase):
    def test_event_name_newline_rejected(self):
        errs = validate_profile(_mutated(event={
            **_BASE["event"], "name": "Real Name\ndifficulty 10"}))
        self.assertTrue(any("event.name" in e and "control character" in e
                            for e in errs), errs)


    def test_domain_name_template_newline_rejected(self):
        errs = validate_profile(_mutated(domain={
            "name_template": "mira-{team}.corp.sus\nname pwned"}))
        self.assertTrue(any("domain.name_template" in e and "control character" in e
                            for e in errs), errs)


# services[].fidelity must be explicit; defaulting it to "exact" claimed unearned parity.
class ServiceFidelity(unittest.TestCase):
    def test_missing_fidelity_rejected(self):
        svc = {k: v for k, v in _BASE["services"][0].items() if k != "fidelity"}
        errs = validate_profile(_mutated(services=[svc]))
        self.assertTrue(any("fidelity must be one of" in e for e in errs), errs)



# REQUIRED_VARS literals must fail here, not at deploy start (nakon_pin_ops) after the golden bundle was built.
class ServiceRequiredVars(unittest.TestCase):
    def test_missing_literal_var_rejected(self):
        svc = {"box": "web01", "name": "hosts redirect", "port": 80,
               "pin": "hosts-redirect-linux", "scored": False, "fidelity": "exact"}
        errs = validate_profile(_mutated(services=[svc]))
        self.assertTrue(any("hosts-redirect-linux" in e and "'HOSTS'" in e
                            for e in errs), errs)





# in_path firewalls own gateway .1; unmanaged .1 without in_path is the legal legacy stand-in shape.
class FirewallBoxes(unittest.TestCase):


    def test_in_path_must_own_the_gateway(self):
        p = copy.deepcopy(_BASE)
        p["boxes"].append({"name": "fw01", "template": "pfsense", "last_octet": 7,
                           "cpu": 1, "memory_mb": 512, "fidelity": "substituted",
                           "unmanaged": True, "in_path": True})
        self.assertTrue(any("last_octet must be 1" in e for e in validate_profile(p)))

    def test_gateway_address_is_firewall_only(self):
        # managed host at .1 is rejected; the legacy unmanaged stand-in stays legal
        p = copy.deepcopy(_BASE)
        p["boxes"][0]["last_octet"] = 1
        p["boxes"][0]["disk_iface"] = "sata0"
        self.assertTrue(any("gateway" in e for e in validate_profile(p)))
        p["boxes"][0]["unmanaged"] = True
        p["boxes"][0].pop("domain_role", None)
        self.assertEqual(validate_profile(p), [])

    def test_second_firewall_rejected(self):
        p = copy.deepcopy(_BASE)
        p["boxes"].append({"name": "fw01", "template": "pfsense", "last_octet": 1,
                           "cpu": 1, "memory_mb": 512, "fidelity": "substituted",
                           "unmanaged": True, "in_path": True})
        p["boxes"].append({"name": "fw02", "template": "pfsense", "last_octet": 254,
                           "cpu": 1, "memory_mb": 512, "fidelity": "substituted",
                           "unmanaged": True, "in_path": True})
        self.assertTrue(any("one per-team lineup" in e for e in validate_profile(p)))


if __name__ == "__main__":
    unittest.main()
