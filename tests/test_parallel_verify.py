"""verify-competition.py's three big remote loops run on a bounded pool, and every
gate still returns the same verdict, the same messages, and the same exit code.

Why these tests exist: verify had ZERO concurrency even though four sibling modules
use utils.run_concurrent. For a 4-team x 5-box range:
  * check_domains was ~20 sequential per-VM probes at 120s (Windows) / 60s (Linux)
    each — 3-10 minutes realistically and up to ~40 minutes in the all-timeout case,
    which is exactly the half-deployed range the gate exists to catch;
  * check_misconfig_survival was a groups x configs x machines loop of 60s SSH probes;
  * report_beacons was one 30s SSH per Linux box, ~20 boxes.

The regression risk of a concurrent rewrite is never "did it get faster" — it is
"did a status, a message, or an exit code change". These tests pin:
  * parallel and re-serialised runs produce byte-identical stdout and the same status
    for representative inputs, including the all-probes-failed case (FAIL, not PASS);
  * the verdict tables the pre-refactor tests encoded (present-on-all PASS,
    present-on-some FAIL, absent-everywhere FAIL, all-unknown SKIP, no domain_roles
    SKIP, duplicate DomainSID FAIL) still hold;
  * exceptions the serial loops let escape still escape;
  * --timeout (new, default 0 = disabled) skips gates as non-passing SKIP and cannot
    turn an unevaluated range into a PASS.
"""

import contextlib
import importlib.util
import io
import itertools
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
_SPEC = importlib.util.spec_from_file_location(
    "verify_parallel_test", _REPO / "verify-competition.py")
verify = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify)

CTX = {"ssh_key_path": "/tmp/key", "vm_username": "scoring",
       "scoring_engine_ip": "10.0.0.1", "box_username": "ubuntu"}


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _run(fn, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = fn(*args, **kwargs)
    return result, out.getvalue()


def _serial_run_concurrent(items, fn, max_workers=8):
    """The pre-W11 shape, used as the control: identical code, one item at a time,
    exceptions captured per slot exactly as utils.run_concurrent captures them."""
    out = []
    for item in items:
        try:
            out.append(fn(item))
        except Exception as exc:  # noqa: BLE001 - mirrors run_concurrent's contract
            out.append(exc)
    return out


# --------------------------------------------------------------------------- domains

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


def _domains_run(boxes, roles, probe, teams=None, serial=False, ctx=None):
    teams = TEAMS if teams is None else teams
    tmp = tempfile.TemporaryDirectory()
    comp_dir = Path(tmp.name)
    (comp_dir / "boxes.json").write_text(json.dumps(boxes))
    (comp_dir / "domain_roles.json").write_text(json.dumps(roles))
    patches = [
        patch.dict(os.environ, {"TF_VAR_proxmox_node": "pve"}),
        patch.object(verify, "guest_agent_exec_windows", side_effect=probe),
        patch.object(verify, "guest_agent_exec_root", side_effect=probe),
    ]
    if serial:
        patches.append(patch.object(verify, "run_concurrent",
                                    side_effect=_serial_run_concurrent))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result, out = _run(verify.check_domains, comp_dir, teams, boxes, ctx=ctx)
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
    """A fully-answering 2-team x 4-box line-up; each case perturbs one probe."""
    out = {verify.vm_id_for(104, 0): dc1, verify.vm_id_for(105, 0): dc2}
    for i in (1, 2, 3):
        out[verify.vm_id_for(104, i)] = member1
        out[verify.vm_id_for(105, i)] = member2
    return out


class DomainsParallelism(unittest.TestCase):
    """check_domains: same verdict and same stdout as the serial loop, overlapping."""

    def _cases(self):
        """(label, responses, expect_status) — representative inputs, including the
        all-probes-failed and duplicate-SID cases the gate exists for."""
        return [
            ("all_valid_distinct", _valid_responses(), verify.Status.PASS),
            ("duplicate_domain_sids",
             _valid_responses(dc2=_dc_out("team105.local", SID_A)), verify.Status.FAIL),
            ("dc_missing_dsid",
             _valid_responses(dc1=_dc_out("team104.local", None)), verify.Status.FAIL),
            ("dc_wrong_dnsroot",
             _valid_responses(dc1=_dc_out("team999.local", SID_A)), verify.Status.FAIL),
            ("member_not_joined",
             _valid_responses(member1="ROLE=2\nDOMAIN=team104.local\nPARTOF=False\n"
                                      "MSID=S-1-5-21-1-2-3-9002\n"), verify.Status.FAIL),
        ]

    def test_parallel_and_serial_agree_on_every_representative_case(self):
        for label, responses, expect in self._cases():
            with self.subTest(case=label):
                probe = _windows_probe(responses)
                par, par_out = _domains_run(WIN_BOXES, ROLES, probe)
                ser, ser_out = _domains_run(WIN_BOXES, ROLES, probe, serial=True)
                self.assertIs(par.status, expect)
                self.assertIs(ser.status, expect)
                self.assertEqual(par.detail, ser.detail)
                self.assertEqual(par_out, ser_out,
                                 "parallel aggregation changed the printed messages")

    def test_duplicate_message_keeps_the_serial_team_order(self):
        probe = _windows_probe({
            verify.vm_id_for(104, 0): _dc_out("team104.local", SID_A),
            verify.vm_id_for(105, 0): _dc_out("team105.local", SID_A),
        })
        result, out = _domains_run(WIN_BOXES, ROLES, probe)
        self.assertIs(result.status, verify.Status.FAIL)
        self.assertIn(f"DomainSID {SID_A} shared by team1, team2", out)

    def test_all_probes_failed_is_a_fail_not_a_pass(self):
        """The half-deployed case: every probe times out/errors. Serial semantics were
        one FAIL line per box and a FAIL verdict — the parallel pass must match."""
        every = {verify.vm_id_for(t, i) for t in (104, 105) for i in range(4)}
        probe = _windows_probe({}, fail=every)
        result, out = _domains_run(WIN_BOXES, ROLES, probe)
        self.assertIs(result.status, verify.Status.FAIL)
        self.assertEqual(out.count("guest-agent probe failed"), 8)  # 2 teams x 4 boxes
        self.assertNotIn("PASS", out)
        _, passed = verify.gate_verdict([result])
        self.assertFalse(passed)

    def test_every_box_is_probed_even_when_one_hangs(self):
        seen = []
        lock = threading.Lock()

        def probe(node, vmid, script, timeout=120):
            with lock:
                seen.append(vmid)
            if vmid == verify.vm_id_for(104, 0):
                raise RuntimeError("agent breaker tripped")
            return 0, _member_out("team104.local"), ""

        _domains_run(WIN_BOXES, ROLES, probe)
        expected = {verify.vm_id_for(t, i) for t in (104, 105) for i in range(4)}
        self.assertEqual(sorted(seen), sorted(expected),
                         "a failing probe stopped the other boxes from being probed")

    def test_linux_agent_failure_falls_back_to_ssh_with_its_info_line(self):
        boxes = [{"name": "web01", "template": "ubuntu-2204-web", "last_octet": 5}]
        roles = {"web01": "member"}
        vmid = verify.vm_id_for(104, 0)

        def probe(node, vmid_, script, timeout=60):
            raise RuntimeError("agent exec breaker tripped")

        def fake_ssh(ctx, ip, cmd, timeout=60):
            return _proc(0, "JOINED=1\n")

        with patch.object(verify, "ssh_via_gateway", side_effect=fake_ssh):
            result, out = _domains_run(boxes, roles, probe, teams={"team1": {"identifier": 104}})
        self.assertIs(result.status, verify.Status.PASS)
        self.assertIn("agent channel unavailable, probed over gateway SSH", out)
        self.assertIn("realm-joined", out)
        self.assertEqual(vmid, verify.vm_id_for(104, 0))

    def test_probes_actually_overlap(self):
        """Serial floor: 8 boxes x 0.25s = 2.0s; the 8-worker pool is ~0.25s."""
        boxes = [{"name": f"win0{i}", "template": "windows-server-2022"} for i in range(1, 9)]
        roles = {b["name"]: ("dc" if i == 0 else "member") for i, b in enumerate(boxes)}
        state = {"cur": 0, "peak": 0}
        lock = threading.Lock()

        def probe(node, vmid, script, timeout=120):
            with lock:
                state["cur"] += 1
                state["peak"] = max(state["peak"], state["cur"])
            time.sleep(0.25)
            with lock:
                state["cur"] -= 1
            if vmid == verify.vm_id_for(104, 0):
                return 0, _dc_out("team104.local", SID_A), ""
            return 0, _member_out("team104.local"), ""

        started = time.monotonic()
        result, _ = _domains_run(boxes, roles, probe, teams={"team1": {"identifier": 104}})
        elapsed = time.monotonic() - started

        self.assertIs(result.status, verify.Status.PASS)
        self.assertLess(elapsed, 1.0,
                        f"domain probes did not overlap ({elapsed:.2f}s for 8x0.25s)")
        self.assertGreater(state["peak"], 1, "domain probes were not concurrent")
        self.assertLessEqual(state["peak"], verify.MAX_CONCURRENCY, "pool exceeded its cap")

    def test_reserialised_control_exceeds_the_threshold(self):
        """Gives the threshold teeth: with run_concurrent replaced by a serial runner
        the same 8 probes cannot come in under 1.0s."""
        boxes = [{"name": f"win0{i}", "template": "windows-server-2022"} for i in range(1, 9)]
        roles = {b["name"]: ("dc" if i == 0 else "member") for i, b in enumerate(boxes)}

        def probe(node, vmid, script, timeout=120):
            time.sleep(0.25)
            if vmid == verify.vm_id_for(104, 0):
                return 0, _dc_out("team104.local", SID_A), ""
            return 0, _member_out("team104.local"), ""

        started = time.monotonic()
        result, _ = _domains_run(boxes, roles, probe, teams={"team1": {"identifier": 104}},
                                 serial=True)
        elapsed = time.monotonic() - started

        self.assertIs(result.status, verify.Status.PASS)
        self.assertGreater(elapsed, 1.0,
                           f"serial control came in under the parallel threshold "
                           f"({elapsed:.2f}s) — the timing test proves nothing")


# ------------------------------------------------------------------ misconfig survival

SURVIVAL_BOXES = [
    {"name": "web01-team104", "ip": "192.168.104.5", "configurations": ["suid-find"]},
    {"name": "web01-team105", "ip": "192.168.105.5", "configurations": ["suid-find"]},
]
PRESENT_OUT = "-rwsr-xr-x 1 root root 12345 /usr/bin/find\n"
ABSENT_OUT = "-rwxr-xr-x 1 root root 12345 /usr/bin/find\n"


def _survival_run(outputs_by_ip, serial=False):
    def fake(ctx, ip, cmd, timeout=60):
        if ip not in outputs_by_ip:
            raise verify.CheckError("ssh: connect refused")
        return _proc(0, outputs_by_ip[ip])

    patches = [patch.object(verify, "ssh_via_gateway", side_effect=fake)]
    if serial:
        patches.append(patch.object(verify, "run_concurrent",
                                    side_effect=_serial_run_concurrent))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return _run(verify.check_misconfig_survival, CTX, SURVIVAL_BOXES)


class MisconfigSurvivalParallelism(unittest.TestCase):
    """The D3/D4 verdict table must survive the rewrite, message for message."""

    def _both(self, outputs):
        par, par_out = _survival_run(outputs)
        ser, ser_out = _survival_run(outputs, serial=True)
        self.assertIs(par.status, ser.status)
        self.assertEqual(par.detail, ser.detail)
        self.assertEqual(par_out, ser_out)
        return par, par_out

    def test_present_on_every_team_passes(self):
        result, out = self._both({"192.168.104.5": PRESENT_OUT, "192.168.105.5": PRESENT_OUT})
        self.assertIs(result.status, verify.Status.PASS)
        self.assertIn("present on all 2 team(s)", out)

    def test_present_on_one_team_fails(self):
        result, out = self._both({"192.168.104.5": PRESENT_OUT, "192.168.105.5": ABSENT_OUT})
        self.assertIs(result.status, verify.Status.FAIL)
        self.assertIn("didn't survive cloning", out)

    def test_absent_everywhere_fails(self):
        result, out = self._both({"192.168.104.5": ABSENT_OUT, "192.168.105.5": ABSENT_OUT})
        self.assertIs(result.status, verify.Status.FAIL)
        self.assertIn("absent on every team", out)

    def test_all_unknown_is_skip_and_not_a_pass(self):
        result, out = self._both({})
        self.assertIs(result.status, verify.Status.SKIP_UNAVAILABLE)
        self.assertIn("SKIP", out)
        _, passed = verify.gate_verdict([result])
        self.assertFalse(passed)

    def test_unexpected_exception_still_escapes(self):
        """The serial loop only caught TimeoutExpired/CheckError; a bug elsewhere must
        not be silently recorded as 'unknown' by the pool."""
        def boom(ctx, ip, cmd, timeout=60):
            raise OSError("something unrelated broke")

        with patch.object(verify, "ssh_via_gateway", side_effect=boom):
            with self.assertRaises(OSError):
                _run(verify.check_misconfig_survival, CTX, SURVIVAL_BOXES)


# --------------------------------------------------------------------------- beacons

BEACON_BOXES = [
    {"name": "web01-team104", "ip": "192.168.104.5", "os": "ubuntu"},
    {"name": "web01-team105", "ip": "192.168.105.5", "os": "ubuntu"},
    {"name": "dc01-team104", "ip": "192.168.104.6", "os": "windows"},
]


class BeaconsParallelism(unittest.TestCase):
    def test_live_unreachable_and_absent_boxes_are_all_reported(self):
        def fake(ctx, ip, cmd, timeout=30):
            if ip == "192.168.104.5":
                return _proc(0, "active\n")
            if ip == "192.168.105.5":
                return _proc(0, "inactive\n")
            raise verify.CheckError("ssh: connect refused")

        with patch.object(verify, "ssh_via_gateway", side_effect=fake):
            _, out = _run(verify.report_beacons, CTX, BEACON_BOXES)
        self.assertIn("LIVE  web01-team104 (192.168.104.5)", out)
        self.assertIn("no beacon unit running (inactive)", out)
        # Windows boxes carry no raw-socket beacon: skipped, and not counted in the
        # denominator either (serial behaviour, unchanged).
        self.assertNotIn("dc01-team104", out)
        self.assertIn("beacons live: 1/2 linux boxes", out)

    def test_unreachable_linux_box_warns_but_does_not_fail(self):
        def fake(ctx, ip, cmd, timeout=30):
            if ip == "192.168.104.5":
                raise verify.CheckError("ssh: connect refused")
            return _proc(0, "active\n")

        with patch.object(verify, "ssh_via_gateway", side_effect=fake):
            _, out = _run(verify.report_beacons, CTX, BEACON_BOXES)
        self.assertIn("WARN  web01-team104 (192.168.104.5): unreachable", out)
        self.assertIn("LIVE  web01-team105 (192.168.105.5)", out)
        self.assertIn("beacons live: 1/2 linux boxes", out)

    def test_unexpected_exception_still_escapes(self):
        def boom(ctx, ip, cmd, timeout=30):
            raise OSError("ssh binary vanished")

        with patch.object(verify, "ssh_via_gateway", side_effect=boom):
            with self.assertRaises(OSError):
                _run(verify.report_beacons, CTX, BEACON_BOXES)


# --------------------------------------------------------------------------- budget

class RunBudgetTests(unittest.TestCase):
    def test_default_is_disabled(self):
        budget = verify.RunBudget(0)
        self.assertFalse(budget.expired())
        self.assertIsNone(budget.deadline)

    def test_a_positive_budget_expires_on_the_clock(self):
        clock = {"t": 100.0}
        with patch.object(verify.time, "monotonic", side_effect=lambda: clock["t"]):
            budget = verify.RunBudget(30)
            self.assertFalse(budget.expired())
            clock["t"] += 31
            self.assertTrue(budget.expired())


class MainBudgetTests(unittest.TestCase):
    """--timeout is additive: it can only ever ADD non-passing SKIPs."""

    def _comp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        comp_dir = Path(tmp.name)
        (comp_dir / "teams.json").write_text(json.dumps(
            {"team1": {"identifier": 104, "password": "pw"}}))
        (comp_dir / "nakon-config.json").write_text(json.dumps({"machines": []}))
        return comp_dir

    def _run_main(self, comp_dir, extra=()):
        patches = [
            patch.object(verify, "read_terraform_ctx",
                         side_effect=verify.CheckError("terraform unavailable")),
            patch.object(verify, "check_logins", return_value=(True, object())),
            patch.object(verify, "check_no_default_creds", return_value=True),
            patch.object(verify, "check_services",
                         return_value=[verify._pass("services", gating=False)]),
            patch.object(verify, "check_isolation", return_value=verify._skip("isolation")),
            patch.object(verify, "report_healthcheck_status"),
            patch.object(verify, "check_misconfig", return_value=True),
            patch.object(verify, "check_misconfig_survival",
                         return_value=verify._pass("misconfig_survival")),
            patch.object(verify, "report_beacons"),
            patch.object(verify, "check_injects", return_value=(False, True)),
            patch.object(verify, "check_round_loop", return_value=verify._pass("round_loop")),
            patch.object(verify, "check_plant_coverage",
                         return_value=verify._pass("plant_coverage")),
            patch.object(verify, "check_domains",
                         return_value=verify._skip("domains", gating=False)),
            patch.object(sys, "argv",
                         ["verify-competition.py", str(comp_dir), "--engine-ip",
                          "10.0.0.1", *extra]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_default_run_never_mentions_a_budget(self):
        comp_dir = self._comp()
        self._run_main(comp_dir)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = verify.main()
        text = out.getvalue()
        self.assertEqual(rc, 1)  # the patched isolation gate is a non-passing SKIP
        self.assertNotIn("run budget", text)
        # ...and the default path still runs every gate.
        verify.check_domains.assert_called_once()  # noqa: SLF001 - patched in _run_main
        verify.check_services.assert_called_once()  # noqa: SLF001

    def test_exhausted_budget_skips_the_remaining_gates_fail_closed(self):
        comp_dir = self._comp()
        self._run_main(comp_dir, extra=["--timeout", "15"])
        # A monotonic clock that advances 10s per query: RunBudget(15) is created on the
        # first query (0), logins runs on the second (10), and everything after is spent.
        ticks = itertools.count()
        with patch.object(verify.time, "monotonic", side_effect=lambda: next(ticks) * 10.0):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = verify.main()
        text = out.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("run budget (--timeout 15s) exhausted", text)
        self.assertIn(f"{'no_default_creds':<18}: SKIP", text)
        self.assertIn(f"{'domains':<18}: SKIP", text)
        self.assertIn("RESULT: FAIL", text)
        # Gates that never ran must not have been called at all.
        verify.check_domains.assert_not_called()  # noqa: SLF001 - patched in _run_main
        verify.check_services.assert_not_called()  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
