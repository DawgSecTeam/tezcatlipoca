"""Mid-event red-loop watchdog (A5).

run-14ac7f14: the red loop stalled twice mid-event and nobody noticed until the
report counted the gaps — the range then sat under no pressure for the better part
of an hour. The unit has no Restart= and a hung loop stays hung, so the scrim's
monitor ticks now cheaply probe red01: unit `inactive`, or `active` with a
world.json older than STALE_AFTER_S (the loop rewrites state every cycle), means
the attack stopped — restart it WITHOUT touching event_start (a mid-event restart
must keep the original clock; only a fresh T0 resets it) and record the recovery
in the run manifest so the report shows a recovered stall, not a phantom healthy
run. Detection must never kill the event: every path is False-tolerant."""

import subprocess
import time

from common import procs
from scrim import core
from scrim.core import log

CHECK_EVERY_S = 300
STALE_AFTER_S = 900      # cycles are minute-scale; 15 min of silence is a hang


def _ssh(args, cmd, timeout):
    from scrim import red_link
    target, common, jump = red_link._red_ssh_ctx(args)
    opts = common + (["-o", jump] if jump else [])
    return procs.run_tree(["ssh", *opts, "-o", "ConnectTimeout=10", target, cmd],
                          timeout=timeout, check=False)


def check_once(args):
    """One probe; returns True only when the loop was restarted (caller counts it)."""
    from scrim import red_link
    unit = red_link.red_unit(args)
    probe = (f"systemctl is-active {unit}; "
             "python3 -c 'import os,time;print(int(time.time()-os.path.getmtime("
             "\"/var/lib/tlaloc/world.json\")))' 2>/dev/null || echo -1")
    try:
        r = _ssh(args, probe, timeout=45)
    except subprocess.TimeoutExpired:
        return False
    lines = (r.stdout or "").split()
    if len(lines) < 2:
        return False                       # ssh flaked; next tick will judge
    state, age = lines[0], lines[1]
    try:
        age_s = int(age)
    except ValueError:
        age_s = -1
    if state == "active" and (age_s < 0 or age_s < STALE_AFTER_S):
        return False                       # alive, or no state file to judge yet
    log(f"RED WATCHDOG: unit {state}, world.json age {age_s}s — restarting {unit} "
        f"(event clock preserved)")
    try:
        rr = _ssh(args, f"sudo -n systemctl restart {unit}; sleep 3; "
                        f"systemctl is-active {unit}", timeout=120)
    except subprocess.TimeoutExpired:
        log("RED WATCHDOG: restart attempt timed out — next tick retries")
        return False
    tail = (rr.stdout or "").strip().splitlines()
    log(f"RED WATCHDOG: restart -> {tail[-1] if tail else 'no output'}")
    return True


def watch_loop(args, stop):
    """Thread body alongside the scoreboard monitor; one restart per stall, checked
    every CHECK_EVERY_S."""
    while not stop.wait(CHECK_EVERY_S):
        try:
            if check_once(args):
                from scrim import run_manifest
                run_manifest.note_red_watchdog_restart(args.run_dir)
        except Exception as e:                                   # noqa: BLE001
            log(f"RED WATCHDOG: probe failed ({type(e).__name__}: {e}) — continuing")
