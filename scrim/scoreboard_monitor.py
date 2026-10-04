import json
import time
from pathlib import Path

from scrim import core
from scrim import llm_probe
from scrim import quotient_api
from scrim import red_link
from scrim.core import log
from scrim.runfiles import SCOREBOARD_STATE


def _cred_team_names(args, creds):
    """Team names this run has creds for, bounded by --teams."""
    return [f"team{i}" for i in range(1, args.teams + 1) if f"TEAM{i}_PW" in creds]


def monitor_loop(args, creds, t0, stop):
    """Snapshot scoreboard + evidence every MONITOR_INTERVAL starting at T+0."""
    sb_path = Path(args.run_dir) / SCOREBOARD_STATE
    teams = _cred_team_names(args, creds)
    while not stop.is_set() and (time.time() - t0) < args.duration_min * 60:
        parsed, texts = {}, {}
        for t in teams:
            try:
                parsed[t] = quotient_api.parsed_status(creds, t)
                texts[t] = quotient_api.render_status(parsed[t])
            except Exception as e:
                texts[t] = f"scoreboard unreachable ({type(e).__name__}: {e})"
        t_plus = int(time.time() - t0)
        if parsed:
            rec = {"t_plus_sec": t_plus, "wallclock": core.now_iso(),
                   "teams": parsed}
            with sb_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        # Context manager: the log append used to be a bare open(...).write(...) with no
        # close, leaking one file descriptor per MONITOR_INTERVAL for the whole event
        # (audit find D5).
        with (Path(args.run_dir) / "monitor.log").open("a") as f:
            f.write(f"\n#### T+{t_plus // 60}min {time.strftime('%H:%M')}\n" +
                    "\n".join(f"{t}:\n{s}" for t, s in texts.items()))
        log("monitor snapshot written")
        if red_link.pull_red_snapshot(args, f"T+{t_plus // 60:03d}"):
            log("red events.jsonl snapshot pulled")
        else:
            log("WARNING: red events.jsonl snapshot unavailable")
        llm_probe.red_llm_watch(args, t_plus)
        stop.wait(core.MONITOR_INTERVAL)
