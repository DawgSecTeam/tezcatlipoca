"""verify's parallel loops must return the same verdicts, messages, and exit codes as the serial loops."""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
import verifier.cli as v_cli  # noqa: E402
import verifier.model as v_model  # noqa: E402
import verifier.verdict as v_verdict  # noqa: E402
from common.targets import vm_id_for  # noqa: E402
import verifier.context as v_context  # noqa: E402
import verifier.domains as v_domains  # noqa: E402
import verifier.engine as v_engine  # noqa: E402
import verifier.isolation as v_isolation  # noqa: E402
import verifier.misconfig as v_misconfig  # noqa: E402
import verifier.scoreboard as v_scoreboard  # noqa: E402
import verifier.state_gates as v_state_gates  # noqa: E402

CTX = {"ssh_key_path": "/tmp/key", "vm_username": "scoring",
       "scoring_engine_ip": "10.0.0.1", "box_username": "ubuntu"}


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _run(fn, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = fn(*args, **kwargs)
    return result, out.getvalue()


# Serial control mirroring utils.run_concurrent's contract, exceptions captured per slot.
def _serial_run_concurrent(items, fn, max_workers=8):
    out = []
    for item in items:
        try:
            out.append(fn(item))
        except Exception as exc:  # noqa: BLE001 - mirrors run_concurrent's contract
            out.append(exc)
    return out


TEAMS = {"team1": {"identifier": 104}, "team2": {"identifier": 105}}
WIN_BOXES = [{"name": f"win0{i}", "template": "windows-server-2022"} for i in range(1, 5)]
ROLES = {"win01": "dc", "win02": "member", "win03": "member", "win04": "member"}
SID_A = "S-1-5-21-3623811012-3361044348-30300820"
SID_B = "S-1-5-21-3623811012-3361044348-30300821"


def _dc_out(domain, dsid=SID_A, svc="true", dnsroot=None):
    return (f"ROLE=4\nDOMAIN={domain}\nPARTOF=True\nMSID=S-1-5-21-1-2-3-9001\n"
            f"DSID={dsid}\nDNSROOT={dnsroot or domain}\nSVC={svc}\n")


def _member_out(domain):
    return f"ROLE=2\nDOMAIN={domain}\nPARTOF=True\nMSID=S-1-5-21-1-2-3-9002\n"


def _domains_run(boxes, roles, probe, teams=None, ctx=None):
    teams = TEAMS if teams is None else teams
    tmp = tempfile.TemporaryDirectory()
    comp_dir = Path(tmp.name)
    (comp_dir / "boxes.json").write_text(json.dumps(boxes))
    (comp_dir / "domain_roles.json").write_text(json.dumps(roles))
    patches = [
        patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "pve"}),
        patch.object(v_domains, "guest_agent_exec_windows", side_effect=probe),
        patch.object(v_domains, "guest_agent_exec_root", side_effect=probe),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result, out = _run(v_domains.check_domains, comp_dir, teams, boxes, ctx=ctx)
    tmp.cleanup()
    return result, out


def _windows_probe(outputs, fail=()):
    def probe(node, vmid, script, timeout=120):
        if vmid in fail:
            raise RuntimeError(f"guest agent exec on vmid {vmid} didn't finish")
        return 0, outputs.get(vmid, ""), ""
    return probe


def _valid_responses(dc1=_dc_out("team104.local", SID_A),
                     dc2=_dc_out("team105.local", SID_B),
                     member1=_member_out("team104.local"),
                     member2=_member_out("team105.local")):
    out = {vm_id_for(104, 0): dc1, vm_id_for(105, 0): dc2}
    for i in (1, 2, 3):
        out[vm_id_for(104, i)] = member1
        out[vm_id_for(105, i)] = member2
    return out


class DomainsParallelism(unittest.TestCase):
    def _cases(self):
        return [
            ("all_valid_distinct", _valid_responses(), v_model.Status.PASS),
            ("duplicate_domain_sids",
             _valid_responses(dc2=_dc_out("team105.local", SID_A)), v_model.Status.FAIL),
            ("dc_missing_dsid",
             _valid_responses(dc1=_dc_out("team104.local", None)), v_model.Status.FAIL),
            ("dc_wrong_dnsroot",
             _valid_responses(dc1=_dc_out("team999.local", SID_A)), v_model.Status.FAIL),
            ("member_not_joined",
             _valid_responses(member1="ROLE=2\nDOMAIN=team104.local\nPARTOF=False\n"
                                      "MSID=S-1-5-21-1-2-3-9002\n"), v_model.Status.FAIL),
        ]

    def test_every_representative_case_gives_its_expected_status(self):
        for label, responses, expect in self._cases():
            with self.subTest(case=label):
                result, _ = _domains_run(WIN_BOXES, ROLES, _windows_probe(responses))
                self.assertIs(result.status, expect)


    # Serial semantics: one FAIL line per box plus a FAIL verdict.
    def test_all_probes_failed_is_a_fail_not_a_pass(self):
        every = {vm_id_for(t, i) for t in (104, 105) for i in range(4)}
        probe = _windows_probe({}, fail=every)
        result, out = _domains_run(WIN_BOXES, ROLES, probe)
        self.assertIs(result.status, v_model.Status.FAIL)
        self.assertEqual(out.count("guest-agent probe failed"), 8)  # 2 teams x 4 boxes
        self.assertNotIn("PASS", out)
        _, passed = v_verdict.gate_verdict([result])
        self.assertFalse(passed)

    def test_every_box_is_probed_even_when_one_hangs(self):
        seen = []
        lock = threading.Lock()

        def probe(node, vmid, script, timeout=120):
            with lock:
                seen.append(vmid)
            if vmid == vm_id_for(104, 0):
                raise RuntimeError("agent breaker tripped")
            return 0, _member_out("team104.local"), ""

        _domains_run(WIN_BOXES, ROLES, probe)
        expected = {vm_id_for(t, i) for t in (104, 105) for i in range(4)}
        self.assertEqual(sorted(seen), sorted(expected),
                         "a failing probe stopped the other boxes from being probed")

    def test_linux_agent_failure_falls_back_to_ssh_with_its_info_line(self):
        boxes = [{"name": "web01", "template": "ubuntu-2204-web", "last_octet": 5}]
        roles = {"web01": "member"}
        vmid = vm_id_for(104, 0)

        def probe(node, vmid_, script, timeout=60):
            raise RuntimeError("agent exec breaker tripped")

        def fake_ssh(ctx, ip, cmd, timeout=60):
            return _proc(0, "JOINED=1\n")

        with patch.object(v_context, "ssh_via_gateway", side_effect=fake_ssh):
            result, out = _domains_run(boxes, roles, probe, teams={"team1": {"identifier": 104}})
        self.assertIs(result.status, v_model.Status.PASS)
        self.assertIn("agent channel unavailable, probed over gateway SSH", out)
        self.assertIn("realm-joined", out)
        self.assertEqual(vmid, vm_id_for(104, 0))



SURVIVAL_BOXES = [
    {"name": "web01-team104", "ip": "192.168.104.5", "configurations": ["suid-find"]},
    {"name": "web01-team105", "ip": "192.168.105.5", "configurations": ["suid-find"]},
]
PRESENT_OUT = "-rwsr-xr-x 1 root root 12345 /usr/bin/find\n"
ABSENT_OUT = "-rwxr-xr-x 1 root root 12345 /usr/bin/find\n"


def _misconfig_run(ctx, boxes):
    with tempfile.TemporaryDirectory() as d:
        return _run(v_misconfig.check_misconfig, ctx, boxes, Path(d))


def _survival_run(outputs_by_ip, serial=False):
    def fake(ctx, ip, cmd, timeout=60):
        if ip not in outputs_by_ip:
            raise v_model.CheckError("ssh: connect refused")
        return _proc(0, outputs_by_ip[ip])

    patches = [patch.object(v_context, "ssh_via_gateway", side_effect=fake)]
    if serial:
        patches.append(patch.object(v_misconfig, "run_concurrent",
                                    side_effect=_serial_run_concurrent))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return _misconfig_run(CTX, SURVIVAL_BOXES)



class MisconfigSurvivalParallelism(unittest.TestCase):
    def _both(self, outputs):
        par, par_out = _survival_run(outputs)
        ser, ser_out = _survival_run(outputs, serial=True)
        self.assertIs(par.status, ser.status)
        self.assertEqual(par.detail, ser.detail)
        self.assertEqual(par_out, ser_out)
        return par, par_out

    def test_present_on_every_team_passes(self):
        result, out = self._both({"192.168.104.5": PRESENT_OUT, "192.168.105.5": PRESENT_OUT})
        self.assertIs(result.status, v_model.Status.PASS)
        self.assertIn("present on all 2 team(s)", out)

    def test_present_on_one_team_fails(self):
        result, out = self._both({"192.168.104.5": PRESENT_OUT, "192.168.105.5": ABSENT_OUT})
        self.assertIs(result.status, v_model.Status.FAIL)
        self.assertIn("didn't survive cloning", out)

    def test_absent_everywhere_fails(self):
        result, out = self._both({"192.168.104.5": ABSENT_OUT, "192.168.105.5": ABSENT_OUT})
        self.assertIs(result.status, v_model.Status.FAIL)
        self.assertIn("absent on every team", out)

    def test_all_unknown_is_skip_and_not_a_pass(self):
        result, out = self._both({})
        self.assertIs(result.status, v_model.Status.SKIP_UNAVAILABLE)
        self.assertIn("SKIP", out)
        _, passed = v_verdict.gate_verdict([result])
        self.assertFalse(passed)

    # Only TimeoutExpired/CheckError are caught; a bug elsewhere must not become 'unknown'.
    def test_unexpected_exception_still_escapes(self):
        def boom(ctx, ip, cmd, timeout=60):
            raise OSError("something unrelated broke")

        with patch.object(v_context, "ssh_via_gateway", side_effect=boom):
            with self.assertRaises(OSError):
                _misconfig_run(CTX, SURVIVAL_BOXES)


BEACON_BOXES = [
    {"name": "web01-team104", "ip": "192.168.104.5", "os": "ubuntu"},
    {"name": "web01-team105", "ip": "192.168.105.5", "os": "ubuntu"},
    {"name": "dc01-team104", "ip": "192.168.104.6", "os": "windows"},
]




if __name__ == "__main__":
    unittest.main()
