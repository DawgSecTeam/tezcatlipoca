"""Routing convergence checks: after apply #1, prove the engine can actually reach
each satellite before anything downstream depends on it (golden plants, nakon,
scoring). The static routes themselves are written into the engine's netplan by the
team_nics provisioner (terraform/main.tf) from the placement's satellite_routes; this
module is the fail-loud gate, not the writer."""

import time

from ssh_ops import ssh_to_engine


def verify_satellite_routing(placement, ctx, timeout=120):
    """For every satellite: the engine must have a route to the anchor team's subnet
    via the jump's mgmt IP, and a live TCP path to the jump's sshd. Raises with the
    offending satellite named — proceeding without routing would strand the satellite
    golden plant (phase 4) dozens of minutes deep into clones."""
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
            f"{last_err}. Check the engine's /etc/netplan/70-satellite-routes.yaml, "
            f"the jump VM's state on the satellite node, and jump mgmt IP "
            f"{', '.join(s['jump_mgmt_ip'] for s in pending)}.")


def _check(placement, ctx, sat):
    anchor = sat.get("anchor_identifier")
    jump_ip = sat["jump_mgmt_ip"]
    if not anchor:
        return False, "satellite has no anchor team (no subnet to route)"
    r = ssh_to_engine(ctx, f"ip route get 192.168.{anchor}.1 2>&1", timeout=15)
    if r.returncode != 0:
        return False, f"ip route get failed: {(r.stderr or r.stdout).strip()[:120]}"
    if f"via {jump_ip}" not in r.stdout:
        return False, (f"engine routes 192.168.{anchor}.1 without the jump "
                       f"(want 'via {jump_ip}', got '{r.stdout.strip()[:120]}')")
    r = ssh_to_engine(
        ctx,
        f"timeout 4 bash -c 'cat < /dev/null > /dev/tcp/{jump_ip}/22' 2>&1 && echo REACHABLE",
        timeout=15)
    if "REACHABLE" not in r.stdout:
        return False, f"engine cannot reach jump {jump_ip}:22 ({(r.stdout or r.stderr).strip()[:120]})"
    return True, ""
