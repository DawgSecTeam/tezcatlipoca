import re
import shlex
import subprocess
import time
from pathlib import Path

from scrim import compworld
from scrim import core
from scrim import procs
from scrim.core import log


WATCHDOG_INTERVAL = 60


def watchdog_script(services, box_pw):
    """Idempotent: for each scored unit that exists and is not active, unmask + enable --now.
    Prints one 'RESTORED <unit>' line per unit it had to bring back."""
    units = []
    for svc in services:
        # box_services entries are strings OR dicts ({name, plant_only, score_only, ...})
        # — the dict form used to raise TypeError (unhashable) in .get / `in` (scrim-one
        # 2026-10-07: win02's plant_only/score_only pins killed the watchdog at T+1min).
        name = svc.get("name") if isinstance(svc, dict) else svc
        if not name:
            continue
        units.extend(compworld._WATCHDOG_UNITS.get(name, []))
    lines = [f"S() {{ echo {shlex.quote(box_pw)} | sudo -S -p '' \"$@\"; }}"]
    for u in dict.fromkeys(units):
        lines.append(
            f"if systemctl list-unit-files {u}.service --no-legend 2>/dev/null | grep -q . "
            f"&& ! systemctl is-active --quiet {u}; then "
            f"S systemctl unmask {u} >/dev/null 2>&1; S systemctl enable --now {u} >/dev/null 2>&1 "
            f"&& echo RESTORED {u}; fi")
    return "\n".join(lines)


def blue_watchdog_loop(args, creds, t0, stop):
    """Non-LLM dead-man's switch (winad-scrim2 rec 7): an account-wide rate limit killed BOTH
    blue agents at once and web01 sat down ~2h, unwatched. Every WATCHDOG_INTERVAL this
    restores any scored Linux unit that is stopped/masked, over the operator's key via the
    engine. It only keeps availability up — it does not hunt or close the entry vector."""
    comp = core.REPO / "competitions" / args.competition
    boxes = core.read_comp_json(comp, "boxes.json")
    box_services = core.read_comp_json(comp, "box_services.json")
    linux = [b for b in boxes if any((s.get("name") if isinstance(s, dict) else s)
                                     in compworld._WATCHDOG_UNITS
                                     for s in box_services.get(b["name"], []))]
    base = core.box_ssh_base(creds, connect_timeout=15)
    wlog = Path(args.run_dir) / "watchdog.log"
    team_ids = sorted(v for k, v in creds.items() if re.fullmatch(r"TEAM\d+_ID", k))
    log(f"blue watchdog on: {[b['name'] for b in linux]} x {len(team_ids)} team(s)")
    while not stop.is_set() and (time.time() - t0) < args.duration_min * 60:
        for tid in team_ids:
            for b in linux:
                host = f"{creds['BOX_USER']}@192.168.{tid}.{b['last_octet']}"
                try:
                    r = procs.run_tree(base + [host, "bash -s"], timeout=60, check=False,
                                       stdin_text=watchdog_script(box_services[b["name"]],
                                                           creds["BOX_PW"]))
                    out = [l for l in (r.stdout or "").splitlines() if l.startswith("RESTORED")]
                    msg = "; ".join(out) if out else ("" if r.returncode == 0 else
                                                      f"ssh rc={r.returncode} {(r.stderr or '').strip()[-120:]}")
                except subprocess.TimeoutExpired:
                    msg = "timeout"
                if msg:
                    line = f"T+{int(time.time() - t0) // 60}min 192.168.{tid}.{b['last_octet']} {b['name']}: {msg}"
                    with wlog.open("a") as f:
                        f.write(line + "\n")
                    log(f"watchdog {line}")
        stop.wait(WATCHDOG_INTERVAL)
