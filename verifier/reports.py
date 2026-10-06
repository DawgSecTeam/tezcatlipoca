"""Informational reports (never gates): range-healthcheck timer status and planted beacons."""

import subprocess

from utils import MAX_CONCURRENCY, PRINT_LOCK, run_concurrent

from verifier import context
from verifier.boxes import is_linux_box
from verifier.model import CheckError


def report_healthcheck_status(ctx):
    """Report range-healthcheck.timer status (informational, not gate)."""
    try:
        proc = context.ssh_to_engine(
            ctx,
            "systemctl is-active range-healthcheck.timer 2>&1; "
            "echo ---; tail -n 5 /var/log/range-healthcheck.log 2>/dev/null"
        )
    except CheckError as e:
        print(f"  (couldn't check range-healthcheck.timer: {e})")
        return
    if proc.returncode != 0 and "inactive" not in proc.stdout and "active" not in proc.stdout:
        print(f"  (couldn't check range-healthcheck.timer: {(proc.stderr or '').strip()[:150]})")
        return
    parts = proc.stdout.split("---", 1)
    status = parts[0].strip()
    tail = parts[1].strip() if len(parts) > 1 else ""
    print(f"  range-healthcheck.timer: {status or 'unknown'}")
    if tail:
        print("  recent log entries:")
        for line in tail.splitlines():
            print(f"      {line}")
    elif status == "active":
        print("  (no recent failures logged)")


def report_beacons(ctx, machines):
    """Report planted raw-socket beacons (informational, not a gate).

    One SSH per Linux box, 30s timeout each; a 4-team x 5-box range is ~20 boxes, i.e.
    up to 10 minutes of sequential waiting for an informational line. The boxes are
    independent and the hop is the engine's ControlMaster-multiplexed channel
    (MaxSessions raised to 64 — utils.run_concurrent's docstring), so it runs on the
    full MAX_CONCURRENCY pool: pure SSH, no Proxmox task, no datastore write. Prints
    stay in the workers under PRINT_LOCK (house pattern: domain_ops/hardening_ops);
    only the informational line order can vary, never a verdict.
    """
    print("\n  (team beacons — informational)")
    linux = [m for m in machines or [] if is_linux_box(m)]
    expected = len(linux)

    def _probe(m):
        ip = m.get("ip")
        name = m.get("name", ip)
        try:
            proc = context.ssh_via_gateway(
                ctx, ip, "systemctl is-active wda-digest.service 2>/dev/null || true", timeout=30)
        except (subprocess.TimeoutExpired, CheckError) as e:
            with PRINT_LOCK:
                print(f"  WARN  {name} ({ip}): unreachable ({e})")
            return False
        state = (proc.stdout or "").strip()
        if state == "active":
            with PRINT_LOCK:
                print(f"  LIVE  {name} ({ip}) — wda-digest.service active")
            return True
        with PRINT_LOCK:
            print(f"  ....  {name} ({ip}) — no beacon unit running ({state or 'none'})")
        return False

    results = run_concurrent(linux, _probe, max_workers=MAX_CONCURRENCY)
    for r in results:
        # The serial loop only caught TimeoutExpired/CheckError; anything else escaped,
        # and run_concurrent's slot now holds it, so it must escape here too.
        if isinstance(r, Exception):
            raise r
    live = sum(1 for r in results if r is True)
    print(f"  beacons live: {live}/{expected} linux boxes")
