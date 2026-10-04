import json
import time
from pathlib import Path

from scrim import core
from scrim import red_link
from scrim.core import log
from scrim.runfiles import ALERTS_FILENAME


# Consecutive from-red01 LLM probe failures before the harness tries to rescue red.
# MONITOR_INTERVAL (300s) per tick: one blip must not trigger a restart that drops a
# tunnel red is mid-decision on, but silence this long is already the dress-rehearsal
# failure mode (an event ran red-LLM-less because a dead `ssh -R` went unnoticed).
RED_LLM_FAIL_THRESHOLD = 3




# endpoint -> {"since": first_failure_ts|None, "failures": int, "restarted": bool}
_llm_watch = {}


def alerts_path(run_dir):
    """Run-dir alert journal (jsonl) that scrim-report.py surfaces."""
    return Path(run_dir) / "evidence" / ALERTS_FILENAME


def write_alert(run_dir, kind, detail, **fields):
    """Append one durable, 0600 alert line; returns the record.

    A lone log() line was the entire record that red spent an event LLM-less (the
    dress rehearsal), because stdout scrolls away and the post-hoc report never reads
    it. Alerts land in the run dir, where the report can count and print them.
    """
    path = alerts_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": core.now_iso(), "kind": kind, "detail": detail,
           **fields}
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    core.secure_evidence(path)
    return rec


def restart_red_llm_tunnel(args):
    """Make ONE rescue attempt on the red-side LLM path; returns a detail string.

    Only a run with a managed reverse tunnel can be rescued from here: with a direct
    (openrouter) endpoint the path is red01's own egress and there is nothing local to
    respawn — say so instead of pretending to have acted.
    """
    tunnel = getattr(args, "red_tunnel", None)
    if not hasattr(tunnel, "restart"):
        return ("no managed reverse tunnel for this endpoint (red egresses directly) — "
                "red stays blind until the endpoint itself recovers")
    try:
        tunnel.restart()
    except Exception as e:
        return f"tunnel respawn FAILED: {type(e).__name__}: {e}"
    return f"reverse tunnel respawned (red01 localhost:{tunnel.remote_port})"


def red_llm_watch(args, t_plus):
    """In-event from-red01 LLM probe: per-endpoint hysteresis plus one rescue per episode.

    The old watcher tracked a single module-global timestamp with no endpoint key and no
    failure count, so it printed one line and never acted — red continued blind for the
    rest of the event (the exact failure the RedTunnel docstring records as having cost a
    dress rehearsal). Here each endpoint carries (first_failure, consecutive_failures,
    restarted): a single blip only logs, RED_LLM_FAIL_THRESHOLD consecutive failures
    trigger ONE tunnel restart and a durable alert, and recovery clears the episode so a
    later outage can be rescued again.
    """
    url = red_link.red_llm_url(args)
    st = _llm_watch.setdefault(url, {"since": None, "failures": 0, "restarted": False})
    if red_link.check_red_llm(args, url):
        if st["failures"]:
            mins = (time.time() - (st["since"] or time.time())) / 60
            log(f"red LLM reachable again after {mins:.0f} min "
                f"({st['failures']} failed probe(s))")
            write_alert(args.run_dir, "red_llm_recovered",
                        f"red01 can reach {url} again after {mins:.0f} min",
                        endpoint=url, failed_probes=st["failures"])
        st.update({"since": None, "failures": 0, "restarted": False})
        return
    st["failures"] += 1
    if st["since"] is None:
        st["since"] = time.time()
    if st["failures"] == 1:
        tunnel = getattr(args, "red_tunnel", None)
        dead = hasattr(tunnel, "proc") and tunnel.proc is not None and tunnel.proc.poll() is not None
        log(f"WARNING: T+{t_plus // 60} — red01 cannot reach the LLM endpoint ({url})"
            + (" (tunnel process dead; supervisor will respawn it)" if dead else "")
            + " — red is making decisions blind until this recovers")
        return
    if st["failures"] < RED_LLM_FAIL_THRESHOLD or st["restarted"]:
        return
    st["restarted"] = True
    detail = restart_red_llm_tunnel(args)
    log(f"WARNING: T+{t_plus // 60} — red01 missed {st['failures']} consecutive LLM probes; "
        f"{detail}")
    write_alert(args.run_dir, "red_llm_restart",
                f"red01 missed {st['failures']} consecutive LLM probes to {url}; {detail}",
                endpoint=url, failed_probes=st["failures"],
                down_since=core.now_iso(st["since"]))
