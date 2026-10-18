"""Per-comp console-only PVE tokens (console): scope, reuse, exact-name teardown."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from infra import console as pco  # noqa: E402


class _Resp:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


def _err(status, text):
    e = requests.HTTPError(f"{status}")
    e.response = _Resp(status, text)
    return e


# Just enough of /access to model what console does (PVE answers 500 for
# missing objects and for duplicates, distinguished only by the message).
class FakePVE:
    def __init__(self, privs=None, roles=None, users=None):
        self.privs = privs if privs is not None else {p: 1 for p in pco.REQUIRED_PRIVS}
        self.roles = set(roles or [])
        self.users = {u: {} for u in (users or [])}
        self.acls = set()
        self.calls = []
        self.n = 0

    def __call__(self, endpoint, token, method, path, **kw):
        self.calls.append((method, path))
        data = kw.get("data") or {}
        if path == "/access/permissions":
            return {"data": {"/": self.privs}}
        if path.startswith("/access/roles"):
            if method == "GET":
                if path.rsplit("/", 1)[-1] not in self.roles:
                    raise _err(500, "role 'TezConsole' does not exist")
                return {"data": {}}
            if data["roleid"] in self.roles:
                raise _err(500, "role already exists")
            self.roles.add(data["roleid"])
            return {"data": None}
        if path == "/access/users":
            if data["userid"] in self.users:
                raise _err(500, "create user failed: user already exists")
            self.users[data["userid"]] = {}
            return {"data": None}
        if path == "/access/acl":
            self.acls.add((data["path"], data["roles"], data["users"]))
            return {"data": None}
        if "/token/" in path:
            user, tid = path.split("/")[3], path.split("/")[5]
            toks = self.users[user]
            if method == "GET":
                if tid not in toks:
                    raise _err(500, "no such token")
                return {"data": {}}
            if method == "DELETE":
                if tid not in toks:
                    raise _err(500, "no such token")
                del toks[tid]
                return {"data": None}
            self.n += 1
            toks[tid] = f"secret-{self.n}"
            return {"data": {"value": toks[tid], "full-tokenid": f"{user}!{tid}"}}
        if method == "DELETE" and path.startswith("/access/users/"):
            user = path.rsplit("/", 1)[-1]
            if user not in self.users:
                raise _err(500, f"delete user failed: user '{user}' does not exist")
            del self.users[user]
            self.acls = {a for a in self.acls if a[2] != user}
            return {"data": None}
        raise AssertionError(f"unexpected {method} {path}")


NODES = {"pve": {"pve_node": "proxmox", "endpoint": "https://203.0.113.10:8006",
                 "token_env": "TOK", "tls_fingerprint": "", "vmids": [1210, 1211, 1220]}}
ENV = {"TOK": "deploy@pam!x=y"}
USER = "tezcon-probe-run-abc@pve"


class Mint(unittest.TestCase):
    def test_acls_are_exactly_this_comps_team_vmids(self):
        pve = FakePVE()
        tokens, problems = pco.mint_console_tokens("probe", "run-abc", NODES, api=pve, env=ENV)
        self.assertEqual(problems, [])
        self.assertEqual(tokens["pve"]["user"], USER)
        self.assertEqual(tokens["pve"]["token_id"], f"{USER}!portal")
        self.assertIn("TezConsole", pve.roles)
        self.assertEqual(pve.acls, {(f"/vms/{v}", "TezConsole", USER) for v in (1210, 1211, 1220)})

    def test_resume_reuses_a_live_token_and_reapplies_acls(self):
        pve = FakePVE()
        first, _ = pco.mint_console_tokens("probe", "run-abc", NODES, api=pve, env=ENV)
        second, _ = pco.mint_console_tokens("probe", "run-abc", NODES, previous=first, api=pve,
                                            env=ENV)
        self.assertEqual(second["pve"]["secret"], first["pve"]["secret"])
        self.assertEqual(pve.n, 1)  # no second token creation


    def test_missing_privileges_degrade_without_touching_access(self):
        pve = FakePVE(privs={"User.Modify": 1})
        tokens, problems = pco.mint_console_tokens("probe", "run-abc", NODES, api=pve, env=ENV)
        self.assertEqual(tokens, {})
        self.assertIn("lacks", problems[0])
        self.assertEqual([c for c in pve.calls if c[0] != "GET"], [])



class Revoke(unittest.TestCase):
    def test_deletes_only_the_exact_user(self):
        neighbours = ["tezcon-probe-run-zzz@pve", "tezcon-probe2-run-abc@pve", "alice@pve"]
        pve = FakePVE(users=neighbours)
        pco.mint_console_tokens("probe", "run-abc", NODES, api=pve, env=ENV)
        self.assertEqual(pco.revoke_console_tokens("probe", "run-abc", NODES, api=pve, env=ENV),
                         [])
        self.assertEqual(sorted(pve.users), sorted(neighbours))
        deletes = [p for m, p in pve.calls if m == "DELETE" and p.startswith("/access/users/")
                   and "/token/" not in p]
        self.assertEqual(deletes, [f"/access/users/{USER}"])

    def test_already_absent_is_clean(self):
        self.assertEqual(pco.revoke_console_tokens("probe", "run-abc", NODES, api=FakePVE(),
                                                   env=ENV), [])



class NodeTargets(unittest.TestCase):
    TARGETS = [{"team_key": "team1", "vmid": 1210}, {"team_key": "team2", "vmid": 1220},
               {"team_key": "team1", "vmid": 1211}]


    def test_multi_node_keys_by_record_name_not_pve_hostname(self):
        placement = {"team_nodes": {"team1": "a", "team2": "b"},
                     "nodes": {"a": {"node": "proxmox", "endpoint": "https://a:8006",
                                     "token_env": "TA"},
                               "b": {"node": "proxmox", "endpoint": "https://b:8006",
                                     "token_env": "TB", "tls_fingerprint": "ff"}}}
        out = pco.node_targets(self.TARGETS, placement, "unused")
        self.assertEqual(out["a"]["vmids"], [1210, 1211])
        self.assertEqual(out["b"]["vmids"], [1220])
        self.assertEqual((out["b"]["endpoint"], out["b"]["tls_fingerprint"]),
                         ("https://b:8006", "ff"))


if __name__ == "__main__":
    unittest.main()
