"""Team-isolation gate: FORWARD DROP rule present AND exercised by a cross-team probe."""

import ipaddress
import re
import subprocess

from verifier import context
from verifier.boxes import is_linux_box
from verifier.model import CheckError, gate_fail, gate_pass, gate_skip


def _team_identifiers(teams):
    """Sorted string identifiers for every team (the third IP octet).

    Tolerates an entry without one: the old `t["identifier"]` raised an uncaught
    KeyError here, killing the whole verifier over a hand-edited teams.json — an
    uncaught crash is the worst failure mode a gate can have."""
    out = set()
    for t in (teams or {}).values():
        ident = t.get("identifier") if isinstance(t, dict) else None
        if ident is not None:
            out.add(str(ident))
    return sorted(out)


def _cidr_covers(cidr, ip):
    """IPv4 CIDR containment — enough for the isolation-rule match."""
    try:
        net_s, bits_s = cidr.split("/")
        bits = int(bits_s)
        net = int(ipaddress.IPv4Address(net_s))
        addr = int(ipaddress.IPv4Address(ip))
    except ValueError:
        return False
    if not 0 <= bits <= 32:
        return False
    mask = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF
    return (net & mask) == (addr & mask)


_ISOLATION_RULE_RE = re.compile(
    r"(?:^|\s)-([sd])\s+(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})(?=\s|$)")


def _has_isolation_rule(stdout, subnet_ips):
    """A FORWARD DROP whose -s AND -d each cover every team subnet.

    The subnets are DERIVED from the team identifiers the rest of the file uses,
    not the hardcoded 192.168.0.0/16 the old match required: a range renumbered
    off that supernet must not read PASS from a match on the old literal (the
    engine's aggregate 192.168.0.0/16 rule covers every derived subnet, so this
    stays a superset check rather than a per-subnet one)."""
    for line in stdout.splitlines():
        if "-j DROP" not in line:
            continue
        sides = {}
        for m in _ISOLATION_RULE_RE.finditer(line):
            sides[m.group(1)] = m.group(2)
        if "s" not in sides or "d" not in sides:
            continue
        if not subnet_ips:
            return True
        if all(_cidr_covers(sides["s"], ip) and _cidr_covers(sides["d"], ip)
               for ip in subnet_ips):
            return True
    return False


def _tcp_probe_cmd(host, port):
    """Remote shell probe: can THIS box open host:port? Prints RC=<exit status>."""
    return f"timeout 3 bash -c 'echo > /dev/tcp/{host}/{port}' 2>/dev/null; echo RC=$?"


def _target_live_from_engine(ctx, to_ip):
    """True/False/None: can the engine itself open to_ip:22?

    The engine's traffic to a directly-attached team subnet is OUTPUT, not FORWARD,
    so the isolation DROP rule cannot apply to it — engine reachability is a
    liveness signal independent of the rule under test."""
    try:
        proc = context.ssh_to_engine(
            ctx, _tcp_probe_cmd(to_ip, 22))
    except (CheckError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return "RC=0" in proc.stdout


def check_isolation(ctx, teams, boxes):
    """Confirm isolation DROP rule present and actually blocks cross-team traffic.

    Returns a GateResult. A rule that exists but couldn't be exercised is SKIP,
    never a pass.

    D5 (live-found 2026-10-02): a target box whose sshd is down, whose VM is
    stopped, or that is otherwise unreachable also yields RC!=0 from the
    cross-team /dev/tcp probe, and the old `blocked = "RC=0" not in stdout` read
    that as "blocked as expected" PASS. Establishing "blocked" now additionally
    requires the engine to reach the target (target is alive, so the failure is
    the rule) and the internet control to pass (the documented "blocked while
    that same box can still reach the internet" condition) — otherwise SKIP."""
    print("\n[3/5] ISOLATION")
    identifiers = _team_identifiers(teams)
    subnets = [f"192.168.{i}.1" for i in identifiers]
    try:
        proc = context.ssh_to_engine(ctx, "sudo iptables -S FORWARD")
    except CheckError as e:
        print(f"  FAIL  — {e}")
        return gate_fail("isolation", f"could not read FORWARD chain ({str(e)[:60]})")
    if proc.returncode != 0:
        print(f"  FAIL  — could not read FORWARD chain (rc={proc.returncode}): "
              f"{(proc.stderr or '').strip()[:150]}")
        return gate_fail("isolation", f"could not read FORWARD chain (rc={proc.returncode})")
    if not _has_isolation_rule(proc.stdout, subnets):
        where = ", ".join(subnets) if subnets else "the team subnets"
        print(f"  FAIL  — no DROP rule covering {where} -> {where} in the FORWARD chain; "
              "teams can currently route to each other through the engine.")
        return gate_fail("isolation", "no team-to-team DROP rule in FORWARD")
    print("  PASS  isolation DROP rule present in FORWARD chain")

    if len(teams) < 2:
        print("  (only 1 team — skipping the cross-team connection test)")
        return gate_pass("isolation", "rule present (single team — probe not applicable)")

    team_ips = {}
    box_by_ip = {}
    for box in boxes:
        parts = box.get("ip", "").split(".")
        if len(parts) != 4 or parts[2] not in identifiers:
            continue
        # Prefer a Linux box per team: the probe authenticates as the linux
        # box_username and Windows boxes want Administrator (a medic probe there
        # always 255s — live-found 2026-09-29 on the dc01-first lineup).
        cur = team_ips.get(parts[2])
        if cur is None or (is_linux_box(box) and not is_linux_box(box_by_ip.get(cur, {}))):
            team_ips[parts[2]] = box["ip"]
        box_by_ip[box["ip"]] = box
    if len(team_ips) < 2:
        print("  (couldn't identify 2 distinct teams' boxes from nakon-config.json — skipping "
          "the cross-team connection test)")
        return gate_pass("isolation", "rule present (couldn't identify two teams' boxes)")
    from_ip, to_ip = (team_ips[i] for i in sorted(team_ips)[:2])

    try:
        proc = context.ssh_via_gateway(
            ctx, from_ip,
            _tcp_probe_cmd(to_ip, 22)
        )
    except (CheckError, subprocess.TimeoutExpired) as e:
        print(f"  SKIP  — cross-team connection test couldn't run ({e}); rule-presence "
              "check above passed, but an untested rule is NOT a verified pass.")
        return gate_skip("isolation", "cross-team probe couldn't run")
    if proc.returncode != 0:
        print(f"  SKIP  — couldn't SSH to {from_ip} to run the test "
              f"(rc={proc.returncode}): {(proc.stderr or '').strip()[:150]}; rule-presence "
              "check above passed, but an untested rule is NOT a verified pass.")
        return gate_skip("isolation", f"couldn't SSH to {from_ip} to run the probe")

    blocked = "RC=0" not in proc.stdout

    try:
        proc2 = context.ssh_via_gateway(
            ctx, from_ip,
            _tcp_probe_cmd("1.1.1.1", 443)
        )
        internet_ok = "RC=0" in proc2.stdout and proc2.returncode == 0
    except (CheckError, subprocess.TimeoutExpired):
        internet_ok = None
    if internet_ok is False:
        print(f"  WARN  {from_ip} can't reach the internet either — the rule (or NAT) may be "
              "over-blocking, not just isolating teams")
    elif internet_ok is None:
        print("  WARN  couldn't run the internet-reachability control check")
    else:
        print(f"  ....  control check ok: {from_ip} can still reach the internet")

    if not blocked:
        print(f"  FAIL  {from_ip} CAN reach {to_ip}:22 — the isolation rule isn't actually "
              "blocking traffic (shadowed or misordered in FORWARD?)")
        return gate_fail("isolation", f"{from_ip} CAN reach {to_ip}:22")

    target_live = _target_live_from_engine(ctx, to_ip)
    if target_live is not True:
        why = ("the engine can't reach it either, so it is dead/stopped"
               if target_live is False else "target liveness couldn't be established")
        print(f"  SKIP  — {from_ip} can't reach {to_ip}:22, but {why} — a dead box and a "
              "blocked one look identical from here, so this is NOT a verified isolation pass.")
        return gate_skip("isolation", f"{from_ip}->{to_ip}:22 failed but target liveness unproven")
    if internet_ok is not True:
        print(f"  SKIP  — {from_ip} can't reach {to_ip}:22 and the internet control "
              f"{'failed' if internet_ok is False else 'could not run'} — the rule may be "
              "over-blocking (or the from-box path is broken), not isolating.")
        return gate_skip("isolation", "target reachable, but the internet control failed")
    print(f"  PASS  {from_ip} cannot reach {to_ip}:22 (blocked as expected; target is live "
          f"and {from_ip} still reaches the internet)")
    return gate_pass("isolation", f"{from_ip}->{to_ip}:22 blocked, target live, control ok")
