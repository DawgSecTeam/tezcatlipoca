"""Red-team vantage gates (--red-identity, --red-teams all): routed source address and per-team reachability."""

import subprocess
import time

from verifier import context
from verifier.boxes import boxes_by_team, is_linux_box
from verifier.context import red_ssh_argv
from verifier.model import CheckError, gate_fail, gate_pass, gate_skip


def default_red_seg_ip(comp_dir=None):
    """--red-seg-ip fallback: the one red segment every range uses."""
    from common.constants import RED_SEGMENT_IP
    return RED_SEGMENT_IP


def check_red_identity(ctx, boxes, red_ip, seg_ip, red_user="sysadmin"):
    """--red-identity: prove red's attack traffic arrives at boxes carrying its red-segment source
    address (routed mode), not the team gateway. Gateway-peer lines in the ss output are the verify
    jump itself — expected, not a red sighting."""
    print(f"\n[+RED] RED IDENTITY (routed-mode source address; red01 {red_ip}, "
          f"expecting source {seg_ip})")
    if not red_ip:
        print("  SKIP  — no red01 address recorded for this run (deploy it, or pass --red-ip)")
        return gate_skip("red_identity", "no red01 address recorded")
    # A satellite team's jump SNATs red to 192.168.<id>.1 by design, so observing the source
    # there is a guaranteed false FAIL: pick a box on a slot-0 team, where red's source
    # survives end-to-end.
    candidates = _engine_node_linux_boxes(ctx, boxes)
    if not candidates:
        print("  SKIP  — no slot-0 Linux box to observe the connection from (ss)")
        return gate_skip("red_identity", "no engine-node Linux box to observe from")
    box_ip = candidates[0]

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
                break  # probe died early
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

    # A positive rc means the remote bash died, i.e. the /dev/tcp connect to the box failed.
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
          "is not preserving its source — the routed FORWARD rules are missing or "
          "shadowed.")
    return gate_fail("red_identity", f"{seg_ip} never visible on {box_ip}")


def _engine_node_linux_boxes(ctx, boxes):
    """Linux box IPs on teams the ENGINE hosts — where red's source survives end-to-end.

    A satellite team's jump SNATs red to 192.168.<id>.1 by design (docs/multi-node.md), so
    observing the red source there is a guaranteed false FAIL.
    """
    from common.targets import frozen_roster
    from common.comp_paths import resolve_comp_dir
    from estate import state as estate_state
    try:
        comp_dir = resolve_comp_dir(comp_dir=(ctx or {}).get("comp_dir")) \
            if (ctx or {}).get("comp_dir") else None
        if comp_dir is None:
            raise OSError("no comp_dir in the engine context")
        placement = estate_state.EstateState.load(comp_dir).data.get("placement") or {}
        engine_node = (placement.get("nodes", {}).get(placement.get("engine_node"))
                       or {}).get("node")
        targets = (frozen_roster(comp_dir) or {}).get("targets") or {}
        engine_teams = {t["team_key"] for t in targets.values()
                        if engine_node and t.get("node") == engine_node}
    except Exception:                                        # noqa: BLE001 - single node/no state
        engine_teams = None
    if not engine_teams:
        return [b["ip"] for b in boxes if b.get("ip") and is_linux_box(b)]
    by_ip = {t["ip"]: t["team_key"] for t in targets.values()}
    return [b["ip"] for b in boxes
            if b.get("ip") and is_linux_box(b)
            and by_ip.get(b["ip"]) in engine_teams]


def check_red_teams(ctx, boxes, red_ip, red_user="sysadmin", timeout=12):
    """--red-teams all: every team must be reachable from red01, not just one — --red-identity
    only proves red reaches one box on its own segment. Fails closed: a team whose boxes all refuse
    the connect is a FAIL, an unreadable team list is a SKIP."""
    print(f"\n[+RED] RED REACHABILITY (red01 {red_ip} -> one box per team)")
    if not red_ip:
        print("  SKIP  — no red01 address recorded for this run (deploy it, or pass --red-ip)")
        return gate_skip("red_teams", "no red01 address recorded")
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
