import contextlib
import json
import os
import re
import socket
import signal
import subprocess
import time
from pathlib import Path

from scrim import blue_helpers
from scrim import blue_prompt
from scrim import core
from scrim import endpoints
from scrim import inject_sync
from scrim import quotient_api
from scrim import test_folder
from scrim.core import log


def stage_blues(args, comp, run_dir, creds, t0):
    endpoints.api_key(local=core.is_local_endpoint(args.blue_base_url))
    for n in range(1, args.teams + 1):
        base_url, blue_model = endpoints.blue_ep(args, n)
        local_blue = core.is_local_endpoint(base_url)
        wd = run_dir / f"blue-team{n}"
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "submissions").mkdir(exist_ok=True)
        tid = creds[f"TEAM{n}_ID"]
        state = core.read_comp_json(comp, ".deploy_state.json")
        credlist = ";".join(f"{u}:{pw}" for u, pw in (state.get("box_creds") or {}).items())
        (wd / "scrim.env").write_text(
            f"ENGINE_IP={creds['ENGINE_IP']}\nMY_TEAM=team{n}\nMY_PW={creds[f'TEAM{n}_PW']}\n"
            f"MY_TID={tid}\nBOX_PW={creds['BOX_PW']}\nINJECT_PW={creds['INJECT_PW']}\n"
            f"KEY_PATH={creds['KEY_PATH']}\nVM_USER={creds['VM_USER']}\nBOX_USER={creds['BOX_USER']}\n"
            f"JAR={quotient_api.jar_path(run_dir, f'team{n}')}\n"
            f"CREDLIST=({credlist})\n")
        os.chmod(wd / "scrim.env", 0o600)
        for helper, body in (("mybox", blue_helpers.mybox_script(comp)), ("scorch", blue_helpers.scorch_script(comp)),
                             ("qlogin", blue_helpers.QLOGIN), ("score.py", blue_helpers.SCORE_PY), ("myscore", blue_helpers.MYSCORE),
                             ("submit-inject", blue_helpers.SUBMIT_INJECT)):
            p = wd / helper
            p.write_text(body)
            p.chmod(0o755)
        core.shutil_copy(comp / "packet.md", wd / "packet.md")
        (wd / "LOG.md").write_text(
            f"# Blue team {n} — defense log\n"
            "# Your after-action report goes in REPORT.md (an operator reads it afterwards)\n")
        if not (wd / "NOTEBOOK.md").exists():
            (wd / "NOTEBOOK.md").write_text(blue_helpers.NOTEBOOK_TEMPLATE.format(n=n))
        (wd / "opencode.jsonc").write_text(
            blue_helpers.OPENCODE_PROJECT_CFG
            .replace("{PROVIDER_KEY}", endpoints.provider_key(base_url))
            .replace("{BASE_URL}", base_url)
            .replace("{API_KEY_FIELD}", "{env:OPENROUTER_API_KEY}" if not local_blue else "local")
            .replace("{MODEL_ID}", blue_model)
            .replace("{CTX}", "60000" if local_blue else "120000")
            .replace("{OUT}", "4000" if local_blue else "16000")
            .replace("{EFFORT}", "" if local_blue else endpoints.effort_json(args.reasoning_effort)))
        log(f"blue workdir team{n} ready ({blue_model} @ {base_url})")
    # Blue's identity and where its deliverables live, recorded while the workdirs are being
    # made: teardown must not have to guess workdir names to collect them (INV5).
    test_folder.record_blue_agents(args, run_dir)


def _opencode_run(args, prompt, wd, env):
    """One blue cycle in a hardened context; the whole process group dies on timeout."""
    m = re.search(r"team(\d+)$", wd.name)
    n_team = int(m.group(1)) if m else 1
    base_url, blue_model = endpoints.blue_ep(args, n_team)
    # subprocess cwd= does not update $PWD and opencode resolves its project
    # (and thus the per-workdir opencode.jsonc defining the scrim-llm provider)
    # from $PWD; it also needs a shell parent to survive — see docs/scrim-harness.md.
    child_env = {**env, "PWD": str(wd), "CYCLE_PROMPT": prompt}
    proc = subprocess.Popen(["bash", "-c",
                             "exec opencode run -m "
                             f"{endpoints.provider_key(base_url)}/{blue_model} --auto \"$CYCLE_PROMPT\""],
                            cwd=wd, env=child_env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=core.CYCLE_TIMEOUT)
    except subprocess.TimeoutExpired:
        # opencode leaves a server grandchild holding the pipes; killing only the
        # direct child made subprocess.run block for that grandchild's lifetime
        # (run-12: one hung cycle held the shared lock ~80 min). bash is the
        # session leader (start_new_session), so killpg takes the whole tree.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _opencode_log_tail(wd):
    """Tail of the newest opencode server log under the team's isolated state."""
    logdir = Path(wd) / ".opencode-home" / "data" / "opencode" / "log"
    try:
        logs = sorted(logdir.glob("*"), key=lambda p: p.stat().st_mtime)
        if logs:
            return ("\n[opencode server log tail]\n"
                    + "\n".join(logs[-1].read_text(errors="replace").splitlines()[-15:]))
    except OSError:
        pass
    return ""


def blue_feed_loop(n, args, creds, t0, stop, llm_lock, first_delay=0.0):
    if first_delay:
        stop.wait(first_delay)
        if stop.is_set():
            return
    while not stop.is_set() and (time.time() - t0) < (args.duration_min - 2) * 60:
        started = time.time()
        elapsed = int((time.time() - t0) / 60)
        remain = args.duration_min - elapsed
        wd = Path(args.run_dir) / f"blue-team{n}"
        home = wd / ".opencode-home"
        for sub in ("data", "config", "cache"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        svc_cfg = home / "config" / "opencode"
        svc_cfg.mkdir(parents=True, exist_ok=True)
        # Fresh OS-assigned port every cycle: the fixed 49370+n collides with any
        # orphaned opencode service left by an earlier run/team on this host
        # (cde-2026 T+0: a Sep-30 orphan held 49371 and burned team1's first cycle).
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            svc_port = sock.getsockname()[1]
        (svc_cfg / "service.json").write_text(json.dumps({"port": svc_port}))
        env = {**os.environ,
               "HOME": str(home), "XDG_DATA_HOME": str(home / "data"),
               "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache")}
        try:
            env["OPENROUTER_API_KEY"] = endpoints.api_key()
        except RuntimeError:
            pass
        try:
            notebook = (wd / "NOTEBOOK.md").read_text()[:2500] if (wd / "NOTEBOOK.md").exists() \
                else "(no notebook yet)"
            try:
                log_tail = "\n".join((wd / "LOG.md").read_text().splitlines()[-5:]) or "(empty)"
            except OSError:
                log_tail = "(no LOG.md yet)"
            prompt = blue_prompt.blue_cycle_prompt(n, creds, args, elapsed, remain,
                                                   blue_prompt.scoreboard_delta(args.run_dir, f"team{n}"),
                                                   inject_sync.inject_brief(creds, f"team{n}"),
                                                   notebook, log_tail)
            cycles = wd / "cycles"
            cycles.mkdir(exist_ok=True)
            r = run_cycle_with_retry(args, prompt, wd, env, llm_lock, stop, team=n)
            out = re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout or "") + (r.stderr or ""))
            if r.returncode != 0:
                out += _opencode_log_tail(wd)
            (cycles / f"cycle-T+{elapsed:03d}.prompt.txt").write_text(prompt)
            (cycles / f"cycle-T+{elapsed:03d}.output.log").write_text(out)
            with (wd / "feed.log").open("a") as f:
                f.write(f"\n===== cycle T+{elapsed} rc={r.returncode} =====\n{out[-2000:]}\n")
            log(f"blue-team{n} cycle T+{elapsed} rc={r.returncode}")
        except subprocess.TimeoutExpired:
            write_cycle_timeout(wd, elapsed)
            log(f"blue-team{n} cycle T+{elapsed} TIMED OUT")
        except Exception as e:
            log(f"blue-team{n} feed error: {e}")
        took = time.time() - started
        stop.wait(min(600, max(30, core.CYCLE_TARGET_PERIOD - took)))


def write_cycle_timeout(wd, elapsed):
    """Record the exact header scrim-report.py counts as a timeout.

    Always written from the timeout path — including when the RETRY is the attempt that
    timed out. The report's `timeouts == 0` rehearsal gate finds this string and nothing
    else, so a timeout that skips it passes the gate silently.
    """
    with (Path(wd) / "feed.log").open("a") as f:
        f.write(f"\n===== cycle T+{elapsed} TIMEOUT =====\n")


def run_cycle_with_retry(args, prompt, wd, env, llm_lock, stop, team=None):
    """One blue cycle, at most two attempts, ONE LOCK ACQUISITION PER ATTEMPT.

    The lock exists to cap concurrency on a shared LLM endpoint. It used to wrap the
    attempt AND its retry, so two hung attempts held it for up to 2 x CYCLE_TIMEOUT
    (1800s each) and starved every other team on the same endpoint of its whole cycle.
    Releasing between attempts lets a waiter run while this team is between attempts;
    the cap on concurrent calls is unchanged. The retry is skipped once the stop event
    is set, so teardown is never held up by a doomed second attempt.
    """
    who = team if team is not None else "?"
    with llm_lock:
        r = _opencode_run(args, prompt, wd, env)
    if r.returncode != 0 and not stop.is_set():
        log(f"blue-team{who} cycle rc={r.returncode} — retrying once (lock released)")
        with llm_lock:
            r = _opencode_run(args, prompt, wd, env)
    return r
