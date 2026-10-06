"""Red-team vantage gates (--red-identity, --red-teams all): routed source address and per-team reachability."""

import json
import subprocess
import time

from verifier import context
from verifier.boxes import boxes_by_team, is_linux_box
from verifier.context import REPO_ROOT, red_ssh_argv
from verifier.model import CheckError, gate_fail, gate_pass, gate_skip


def default_red_seg_ip():
    """--red-seg-ip fallback: bad-auto's config.yaml, else the routed default."""
    cfg_path = REPO_ROOT.parent / "bad-auto" / "config.yaml"
    try:
        deploy = json.loads(cfg_path.read_text()).get("deploy") or {}
        return deploy.get("red_seg_ip") or "10.200.0.10"
    except (OSError, ValueError):
        return "10.200.0.10"


def check_red_identity(ctx, boxes, red_ip, seg_ip, red_user="sysadmin"):
    """--red-identity: prove red's attack traffic arrives at boxes carrying
    its red-segment source address (routed mode), not the team gateway the
    scoring checks source from. Holds a TCP connection open from red01 to a
    Linux box's :22 and reads the box's connection table (ss) while it's up.

    Returns a GateResult; unprovable paths are SKIP, never a pass. Gateway-peer
    lines in the ss output are the verify jump itself (ProxyCommand enters
    through the engine) — expected, not a red sighting."""
    print(f"\n[+RED] RED IDENTITY (routed-mode source address; red01 {red_ip}, "
          f"expecting source {seg_ip})")
    linux_ips = [b["ip"] for b in boxes
                 if b.get("ip") and is_linux_box(b)]
    if not linux_ips:
        print("  SKIP  — no Linux box to observe the connection from (ss)")
        return gate_skip("red_identity", "no Linux box to observe from")
    box_ip = linux_ips[0]

    try:
        hold = subprocess.Popen(
            red_ssh_argv(ctx, red_user, red_ip,
                         f"timeout 25 bash -c 'exec 3<>/dev/tcp/{box_ip}/22; sleep 22'"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        print(f"  SKIP  — couldn't spawn the red01 probe ssh: {e}")
        return gate_skip("red_identity", "couldn't spawn the red01 probe ssh")

    try:
        observed = ""
        for _ in range(5):
            if hold.poll() is not None:
                break  # probe died early — red01 unreachable or box refused
            time.sleep(2)
            try:
                proc = context.ssh_via_gateway(
                    ctx, box_ip, "ss -tn state established '( sport = :22 )'",
                    timeout=20)
            except (CheckError, subprocess.TimeoutExpired) as e:
                print(f"  SKIP  — couldn't read {box_ip}'s connection table: {e}")
                return gate_skip("red_identity", f"couldn't read {box_ip}'s connection table")
            if proc.returncode != 0:
                continue
            observed = proc.stdout
            if seg_ip in observed:
                break
        if hold.poll() is None:
            hold.kill()
    finally:
        hold.wait()

    # rc 0/-9/-15/-None: connected (or we killed the holder mid-sleep — fine).
    # A positive rc means the remote bash died, i.e. the /dev/tcp connect to
    # the box failed — red cannot reach the team subnet at all.
    if hold.returncode is not None and hold.returncode > 0:
        print(f"  FAIL  — red01 could not open the probe connection to "
              f"{box_ip}:22 at all (probe rc={hold.returncode}): red cannot "
              "reach the team subnet, routed firewall rules missing/wrong?")
        return gate_fail("red_identity", "red01 could not reach the team subnet")
    if seg_ip in observed:
        print(f"  PASS  {seg_ip} visible on {box_ip} as an established :22 peer — "
              "red's source address survives end-to-end")
        return gate_pass("red_identity", f"{seg_ip} visible end-to-end")
    print(f"  FAIL  — {seg_ip} never appeared among {box_ip}'s established :22 "
          f"peers while red01 held a connection open. Observed peers:\n"
          f"{observed.strip() or '    (none)'}\n"
          "        If the only peers are the team gateway (192.168.<tid>.1), red "
          "is still masqueraded — bad-auto deployed in masq mode or the routed "
          "FORWARD rules are shadowed.")
    return gate_fail("red_identity", f"{seg_ip} never visible on {box_ip}")


def check_red_teams(ctx, boxes, red_ip, red_user="sysadmin", timeout=12):
    """--red-teams all: every team must be reachable from red01, not just one.

    scale8-soak-2026-10-02: routed red01 could not reach ANY satellite team box
    ("unreachable over SSH" on 15/15 cred_sprays) and this went unnoticed until
    T+40 because `--red-identity` only proves red reaches one box on its own
    segment. Red's whole job depends on this path, so it is a gate, and it runs
    before T0 in the scrim's stage_verify.

    Probes each team's boxes in turn and stops at the first that answers, so a
    team costs one round trip when it is healthy. Fails closed: a team whose boxes
    all refuse the connect is a FAIL, and an unreadable team list is a SKIP (never
    a pass), matching the discipline of every other gate in this file."""
    print(f"\n[+RED] RED REACHABILITY (red01 {red_ip} -> one box per team)")
    by_team = boxes_by_team(boxes)
    if not by_team:
        print("  SKIP  — no team boxes with usable IPs in the machine list")
        return gate_skip("red_teams", "no team boxes with usable IPs")

    unreachable, reached = [], []
    for ident in sorted(by_team, key=int):
        candidates = sorted(by_team[ident], key=lambda b: str(b.get("name") or ""))
        hit = None
        for box in candidates:
            ip = box["ip"]
            try:
                proc = subprocess.run(
                    red_ssh_argv(ctx, red_user, red_ip,
                                 f"timeout {timeout} bash -c 'exec 3<>/dev/tcp/{ip}/22' && echo RED-OK"),
                    capture_output=True, text=True, timeout=timeout + 20)
            except (OSError, subprocess.SubprocessError) as e:
                print(f"  SKIP  — couldn't run the red01 probe: {e}")
                return gate_skip("red_teams", "couldn't run the red01 probe")
            if "RED-OK" in (proc.stdout or ""):
                hit = ip
                break
        if hit:
            reached.append(f"team{ident}({hit})")
            print(f"  ok    team{ident}: red01 reached {hit}:22")
        else:
            unreachable.append(f"team{ident}")
            tried = ", ".join(f"{b['ip']}:22" for b in candidates)
            print(f"  FAIL  team{ident}: red01 could not open a TCP connection to any of "
                  f"{tried}")

    if unreachable:
        print(f"  FAIL  — red01 cannot reach {len(unreachable)}/{len(by_team)} team(s): "
              f"{', '.join(unreachable)}. Red cannot attack what it cannot dial: check the "
              "satellite jumps' FORWARD/SNAT rules for the red segment (TEZ_RED_SEGMENT) "
              "and the engine's routes to every team subnet.")
        return gate_fail("red_teams", f"red01 unreachable for {', '.join(unreachable)}")
    print(f"  PASS  red01 reached {len(reached)}/{len(by_team)} teams")
    return gate_pass("red_teams", f"red01 reached all {len(reached)} teams")
