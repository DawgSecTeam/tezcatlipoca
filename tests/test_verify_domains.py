"""check_domains fails closed: DSID presence/shape, role-file schema, box names.

The DC branch must reject missing/malformed DomainSIDs before the uniqueness
check (with one team, uniqueness alone is vacuous), and a present-but-invalid
domain_roles.json must FAIL, never silently skip boxes or crash the verifier."""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
_SPEC = importlib.util.spec_from_file_location(
    "verify_domains_test", _REPO / "verify-competition.py")
verify = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify)
import verifier.domains as v_domains  # noqa: E402

TEAMS = {"team1": {"identifier": 104}, "team2": {"identifier": 105}}
BOXES = [
    {"name": "dc01", "template": "windows-server-2022"},
    {"name": "win01", "template": "windows-server-2022"},
    {"name": "web01", "template": "ubuntu-2204-web"},
]
SID1 = "S-1-5-21-3623811012-3361044348-30300820"
SID2 = "S-1-5-21-3623811012-3361044348-30300821"


def _probe_output(domain, dsid=None, svc="true", dnsroot=None, aderr=None):
    out = (f"ROLE=4\nDOMAIN={domain}\nPARTOF=True\n"
           "MSID=S-1-5-21-1-2-3-9001\n")
    if not aderr:
        out += f"DSID={dsid}\nDNSROOT={dnsroot or domain}\nSVC={svc}\n"
    else:
        out += f"ADERR={aderr}\n"
    return out


class CheckDomainsTests(unittest.TestCase):
    def _comp_dir(self, roles_text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        comp_dir = Path(tmp.name)
        (comp_dir / "boxes.json").write_text(json.dumps(BOXES))
        if roles_text is not None:
            (comp_dir / "domain_roles.json").write_text(roles_text)
        return comp_dir

    def _run(self, comp_dir, responses, teams=TEAMS):
        outs = {}

        def probe(node, vmid, script, timeout=120):
            return 0, outs.setdefault(vmid, ""), ""

        for vmid, out in responses.items():
            outs[vmid] = out
        stdout = io.StringIO()
        with patch.dict(os.environ, {"TF_VAR_proxmox_node": "node"}), \
             patch.object(v_domains, "guest_agent_exec_windows", side_effect=probe), \
             patch.object(v_domains, "guest_agent_exec_root", side_effect=probe), \
             contextlib.redirect_stdout(stdout):
            result = verify.check_domains(comp_dir, teams, BOXES)
        return result, stdout.getvalue()

    def test_two_teams_valid_distinct_dsids_pass(self):
        comp_dir = self._comp_dir('{"dc01": "dc", "win01": "member", "web01": "member"}')
        responses = {
            1240: _probe_output("team104.local", SID1),
            1250: _probe_output("team105.local", SID2),
            1241: "ROLE=2\nDOMAIN=team104.local\nPARTOF=True\nMSID=S-1-5-21-1-2-3-9002\n",
            1251: "ROLE=2\nDOMAIN=team105.local\nPARTOF=True\nMSID=S-1-5-21-1-2-3-9002\n",
            1242: "JOINED=1\n",
            1252: "JOINED=1\n",
        }
        result, out = self._run(comp_dir, responses)
        self.assertTrue(result.passed)
        self.assertIn("all DomainSIDs unique", out)
        self.assertIn("shared by", out)

    def test_duplicate_dsids_fail(self):
        comp_dir = self._comp_dir('{"dc01": "dc"}')
        responses = {
            1240: _probe_output("team104.local", SID1),
            1250: _probe_output("team105.local", SID1),
        }
        result, out = self._run(comp_dir, responses)
        self.assertFalse(result.passed)
        self.assertIn(f"DomainSID {SID1} shared by team1, team2", out)

    def test_one_team_pass_reports_uniqueness_not_exercised(self):
        comp_dir = self._comp_dir('{"dc01": "dc"}')
        result, out = self._run(comp_dir, {1240: _probe_output("team104.local", SID1)},
                                teams={"team1": {"identifier": 104}})
        self.assertTrue(result.passed)
        self.assertIn("uniqueness needs a second team", out)

    def test_missing_dsid_fails(self):
        comp_dir = self._comp_dir('{"dc01": "dc"}')
        result, out = self._run(comp_dir, {1240: _probe_output("team104.local", None)})
        self.assertFalse(result.passed)
        self.assertIn("no valid DomainSID", out)
        self.assertNotIn("shared by", out)

    def test_malformed_dsid_fails(self):
        comp_dir = self._comp_dir('{"dc01": "dc"}')
        result, out = self._run(
            comp_dir, {1240: _probe_output("team104.local", "S-1-5-700")})
        self.assertFalse(result.passed)
        self.assertIn("no valid DomainSID", out)

    def test_account_style_dsid_with_rid_fails(self):
        comp_dir = self._comp_dir('{"dc01": "dc"}')
        result, out = self._run(
            comp_dir,
            {1240: _probe_output("team104.local", "S-1-5-21-1-2-3-500")})
        self.assertFalse(result.passed)
        self.assertIn("no valid DomainSID", out)

    def test_unexpected_dc_dnsroot_fails(self):
        comp_dir = self._comp_dir('{"dc01": "dc"}')
        result, out = self._run(
            comp_dir, {1240: _probe_output("team999.local", SID1)})
        self.assertFalse(result.passed)
        self.assertIn("DC not serving team104.local", out)

    def test_unknown_role_box_name_fails(self):
        comp_dir = self._comp_dir('{"dc01": "dc", "ghost01": "member"}')
        result, out = self._run(comp_dir, {})
        self.assertFalse(result.passed)
        self.assertIn("absent from boxes.json: ghost01", out)

    def test_invalid_role_value_fails(self):
        comp_dir = self._comp_dir('{"dc01": "DC", "win01": "member"}')
        result, out = self._run(comp_dir, {})
        self.assertFalse(result.passed)
        self.assertIn("invalid role value(s): dc01='DC'", out)

    def test_non_dict_roles_fail(self):
        comp_dir = self._comp_dir('["dc01"]')
        result, out = self._run(comp_dir, {})
        self.assertFalse(result.passed)
        self.assertIn("must map box names", out)

    def test_malformed_roles_json_fails(self):
        comp_dir = self._comp_dir('{"dc01": ')
        result, out = self._run(comp_dir, {})
        self.assertFalse(result.passed)
        self.assertIn("unreadable/malformed", out)

    def test_absent_roles_skips(self):
        result, out = self._run(self._comp_dir(None), {})
        self.assertIs(result.status, verify.Status.SKIP_UNAVAILABLE)
        self.assertIn("SKIP", out)


if __name__ == "__main__":
    unittest.main()
