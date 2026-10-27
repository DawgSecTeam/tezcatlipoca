"""Fail-loud routing convergence gate; the static routes themselves are written by the team-NIC netplan step from satellite_routes."""

import time

from common.ssh_ops import ssh_on_gateway


def verify_satellite_routing(placement, ctx, timeout=120):
    sats = placement.get("satellites") or []
    if not sats:
        return
    deadline = time.time() + timeout
    pending = list(sats)
    last_err = ""
    while pending and time.time() < deadline:
        remaining = []
        for sat in pending:
            ok, why = _check(placement, ctx, sat)
            if ok:
                print(f"    routing {sat['name']}: engine -> {sat['jump_mgmt_ip']} "
                      f"via-route confirmed")
            else:
                last_err = why
                remaining.append(sat)
        pending = remaining
        if pending:
            time.sleep(5)
    if pending:
        names = ", ".join(s["name"] for s in pending)
        raise RuntimeError(
            f"routing to satellite(s) {names} never converged within {timeout}s: "
            f"{last_err}. Check the engine's /usr/local/sbin/satellite-routes.sh and "
            f"satellite-routes.service (an engine reboot re-asserts them), "
            f"the jump VM's state on the satellite node, and jump mgmt IP "
            f"{', '.join(s['jump_mgmt_ip'] for s in pending)}.")


def _check(placement, ctx, sat):
    anchor = sat.get("anchor_identifier")
    jump_ip = sat["jump_mgmt_ip"]
    if not anchor:
        return False, "satellite has no anchor team (no subnet to route)"
    r = ssh_on_gateway(ctx, f"ip route get 192.168.{anchor}.1 2>&1", timeout=15)
    if r.returncode != 0:
        return False, f"ip route get failed: {(r.stderr or r.stdout).strip()[:120]}"
    if f"via {jump_ip}" not in r.stdout:
        return False, (f"engine routes 192.168.{anchor}.1 without the jump "
                       f"(want 'via {jump_ip}', got '{r.stdout.strip()[:120]}')")
    r = ssh_on_gateway(
        ctx,
        f"timeout 4 bash -c 'cat < /dev/null > /dev/tcp/{jump_ip}/22' 2>&1 && echo REACHABLE",
        timeout=15)
    if "REACHABLE" not in r.stdout:
        return False, f"engine cannot reach jump {jump_ip}:22 ({(r.stdout or r.stderr).strip()[:120]})"
    return True, ""
