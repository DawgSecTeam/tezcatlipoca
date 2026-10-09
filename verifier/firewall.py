"""In-path firewall gate: after phase 5's cutover each team's traffic must still ride its
firewall (pfSense or VyOS — the probes here are OS-agnostic).

Phase 5 proves this once (firewall_ops.verify_in_path). This gate re-proves it on every verify so
drift is caught — chiefly an out-of-band `terraform apply` that rewrites the pre-cutover engine
netplan and puts the team gateway address back on the engine (docs/known-issues.md)."""

import json
import subprocess

from firewall_ops import firewall_wan_ip, tcp_probe_cmd, team_gateway_ip
from utils import is_in_path_fw

from verifier import context
from verifier.isolation import _team_identifiers
from verifier.model import CheckError, gate_fail, gate_pass, gate_skip


def has_in_path_firewall(comp_dir):
    try:
        boxes = json.loads((comp_dir / "boxes.json").read_text())
    except (OSError, ValueError):
        return False
    return any(is_in_path_fw(b) for b in boxes)


def _engine(ctx, cmd):
    """(rc, stdout) of one engine command; CheckError/timeout surface as CheckError."""
    try:
        proc = context.ssh_to_engine(ctx, cmd)
    except subprocess.TimeoutExpired as e:
        raise CheckError(f"engine SSH timed out ({e})")
    return proc.returncode, proc.stdout or ""


def _team_problems(ctx, tid):
    wan, gw = firewall_wan_ip(tid), team_gateway_ip(tid)
    problems = []
    rc, out = _engine(ctx, f"ip route get {gw}")
    if rc != 0 or f"via {wan}" not in out:
        problems.append(f"engine routes {gw} directly, not via {wan} ({out.strip()[:80]})")
    rc, out = _engine(ctx, tcp_probe_cmd(wan, 22, timeout=5))
    if "UP" not in out:
        problems.append(f"firewall WAN {wan}:22 does not answer from the engine")
    rc, out = _engine(ctx, "ip -4 -o addr show")
    if f"inet {gw}/" in out:
        problems.append(f"engine still holds the team gateway {gw} (pre-cutover netplan "
                        "re-applied?) — traffic bypasses the firewall")
    return problems


def check_firewall_in_path(ctx, comp_dir, teams):
    """GateResult: SKIP without an in_path firewall; FAIL on any per-team drift; SKIP when the
    engine can't be reached (an unrun check is not a pass)."""
    if not has_in_path_firewall(comp_dir):
        return gate_skip("firewall_in_path", "no in_path firewall in this competition",
                         gating=False)
    print("\n  (in-path firewall — route via transit, firewall answers, engine gateway released)")
    failures = []
    try:
        for tid in _team_identifiers(teams):
            for problem in _team_problems(ctx, tid):
                print(f"  FAIL  team {tid}: {problem}")
                failures.append(f"team {tid}: {problem}")
    except CheckError as e:
        print(f"  SKIP  — could not reach the engine to check the firewall path ({e})")
        return gate_skip("firewall_in_path", "engine unreachable")
    if failures:
        return gate_fail("firewall_in_path", "; ".join(failures)[:200])
    print("  PASS  every team routes via its firewall; engine no longer holds the gateway")
    return gate_pass("firewall_in_path")
