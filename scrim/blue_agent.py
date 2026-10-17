import contextlib
import json
import shlex
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
from scrim import llm_endpoints
from scrim import inject_sync
from scrim import quotient_api
from scrim import test_folder
from scrim.core import log


LOCAL_BLUE_CTX = 60000
# A cycle is watched in short slices so the event deadline can abort it: the supervisor's
# join past the deadline is capped by the window (scrim/supervisor.py), and a cycle that
# ignored stop for the rest of its CYCLE_TIMEOUT would keep the driver alive past it.
CYCLE_POLL_SEC = 1.0


def local_endpoint_n_ctx(base_url, timeout=5):
    import urllib.request
    root = re.sub(r"/v1/?$", "", base_url.rstrip("/"))
    try:
        with urllib.request.urlopen(f"{root}/props", timeout=timeout) as r:
            props = json.load(r)
        return int((props.get("default_generation_settings") or {}).get("n_ctx")
                   or props.get("n_ctx"))
    except (OSError, ValueError, TypeError):
        return None


# opencode's own prompt is ~20k tokens: advertising more context than the endpoint really
# has makes opencode self-compact fatally. Unreadable /props only warns.
def assert_local_ctx(base_url):
    n_ctx = local_endpoint_n_ctx(base_url)
    if n_ctx is None:
        log(f"WARNING: could not read n_ctx from {base_url}/props — "
            f"assuming it holds the advertised {LOCAL_BLUE_CTX}")
    elif n_ctx < LOCAL_BLUE_CTX:
        raise SystemExit(f"local blue endpoint {base_url} has n_ctx={n_ctx}, below the "
                         f"advertised {LOCAL_BLUE_CTX} — restart llama.cpp with a larger -c")


def credlist_literal(box_creds):
    """`CREDLIST=(...)` for scrim.env: a bash array of quoted "user:password" pairs.

    A bash ARRAY — the helpers iterate "${CREDLIST[@]}". ";"-separated entries inside `( )`
    are a syntax error, so `source ./scrim.env` failed for EVERY helper and each blue cycle
    spent its first minutes repairing the harness by hand (a blue team's own notebook
    recorded it). Each pair is quoted for the password's punctuation.
    """
    return "(" + " ".join(shlex.quote(f"{u}:{pw}") for u, pw in box_creds.items()) + ")"


def stage_blues(args, comp, run_dir, creds, t0):
    log(f"blue endpoint: {llm_endpoints.describe(args.blue_base_url)}")
    endpoints.api_key(args.blue_base_url)     # public entries raise when their key is missing
    for n in range(1, args.teams + 1):
        base_url, blue_model = endpoints.blue_ep(args, n)
        local_blue = core.is_local_endpoint(base_url)
        if local_blue:
            assert_local_ctx(base_url)   # llama.cpp-only /props probe; never for a public API
        wd = run_dir / f"blue-team{n}"
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "submissions").mkdir(exist_ok=True)
        tid = creds[f"TEAM{n}_ID"]
        state = core.read_state(comp)
        # A bash ARRAY (the helpers iterate "${CREDLIST[@]}"): ";"-separated entries inside
        # `( )` are a syntax error, so `source ./scrim.env` failed for EVERY helper and each
        # blue cycle spent its first minutes repairing the harness by hand (caught live,
        # A blue team's notebook). Quote each pair for the password's punctuation.
        credlist = credlist_literal(state.get("box_creds") or {})
        (wd / "scrim.env").write_text(
            f"ENGINE_IP={creds['ENGINE_IP']}\nMY_TEAM=team{n}\nMY_PW={creds[f'TEAM{n}_PW']}\n"
            f"MY_TID={tid}\nBOX_PW={creds['BOX_PW']}\nINJECT_PW={creds['INJECT_PW']}\n"
            f"KEY_PATH={creds['KEY_PATH']}\nVM_USER={creds['VM_USER']}\nBOX_USER={creds['BOX_USER']}\n"
            f"JAR={quotient_api.jar_path(run_dir, f'team{n}')}\n"
            f"CREDLIST={credlist}\n")
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
        key_env = endpoints.key_env_name(base_url)
        (wd / "opencode.jsonc").write_text(
            blue_helpers.OPENCODE_PROJECT_CFG
            .replace("{PROVIDER_KEY}", endpoints.provider_key(base_url))
            .replace("{BASE_URL}", base_url)
            # a public entry names its own key env; only an operator-side endpoint gets
            # the literal "local" (llama.cpp ignores the header entirely)
            .replace("{API_KEY_FIELD}", ("{env:%s}" % key_env) if key_env else "local")
            .replace("{MODEL_ID}", blue_model)
            .replace("{CTX}", str(LOCAL_BLUE_CTX) if local_blue else "120000")
            .replace("{OUT}", "4000" if local_blue else "16000")
            .replace("{EFFORT}", "" if local_blue else endpoints.effort_json(args.reasoning_effort)))
        log(f"blue workdir team{n} ready ({blue_model} @ {base_url})")
    # Blue's identity and where its deliverables live, recorded while the workdirs are being
    # made: teardown must not have to guess workdir names to collect them (INV5).
    test_folder.record_blue_agents(args, run_dir)


def _opencode_run(args, prompt, wd, env, stop=None):
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
    deadline = time.time() + core.CYCLE_TIMEOUT
    while True:
        try:
            out, err = proc.communicate(
                timeout=min(CYCLE_POLL_SEC, max(0.05, deadline - time.time())))
            return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            # a retried communicate loses no output (documented Popen behavior); the cycle
            # ends on its own budget OR on the event deadline, whichever comes first
            if time.time() >= deadline or (stop is not None and stop.is_set()):
                break
    # opencode leaves a server grandchild holding the pipes; killing only the
    # direct child made subprocess.run block for that grandchild's lifetime
    # (run-12: one hung cycle held the shared lock ~80 min). bash is the
    # session leader (start_new_session), so killpg takes the whole tree.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    out, err = proc.communicate()
    # ...but opencode's managed `serve --service` grandchild starts its OWN session,
    # so killpg leaves it holding the cycle's port and the NEXT cycle dies on
    # "port already in use" (caught live: a 4 h orphan squatted 49374 and
    # every later cycle failed). HOME is per-cycle and unique, so it identifies it.
    _reap_managed_servers(child_env.get("HOME", ""))
    try:
        (wd / "cycles").mkdir(exist_ok=True)
        (wd / "cycles" / "last-timeout.partial.log").write_text(
            re.sub(r"\x1b\[[0-9;]*m", "", (out or "") + (err or ""))[-4000:])
    except OSError:
        pass
    raise subprocess.TimeoutExpired(proc.args, core.CYCLE_TIMEOUT)


def _reap_managed_servers(home):
    """SIGKILL leftover `opencode serve --service` processes started under `home`."""
    if not home:
        return
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (p / "cmdline").read_bytes()
            if b"--service" not in cmd or b"opencode" not in cmd:
                continue
            envb = (p / "environ").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        if f"HOME={home}" in envb.split("\0"):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(int(p.name), signal.SIGKILL)


def _opencode_log_tail(wd):
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
        # (one team's first cycle was burned by exactly such an orphan).
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            svc_port = sock.getsockname()[1]
        (svc_cfg / "service.json").write_text(json.dumps({"port": svc_port}))
        # The child is an unattended model with an auto-approved shell. Handing it
        # the driver's environment hands it the Proxmox token and the dashboard
        # bearer; only the OS/terminal variables and the one provider key it needs
        # cross the boundary.
        _KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "SHELL",
                 "TMPDIR", "USER", "LOGNAME")
        env = {name: os.environ[name] for name in _KEEP if name in os.environ}
        env.update({"HOME": str(home), "XDG_DATA_HOME": str(home / "data"),
                    "XDG_CONFIG_HOME": str(home / "config"),
                    "XDG_CACHE_HOME": str(home / "cache")})
        try:
            # the same key the workdir's config asks opencode for, exported where opencode
            # reads env ({env:...} in opencode.jsonc resolves against this process)
            _base_url, _ = endpoints.blue_ep(args, n)
            _key_env = endpoints.key_env_name(_base_url) or "OPENROUTER_API_KEY"
            env[_key_env] = endpoints.api_key(_base_url)
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


# the exact header scrim-report.py counts as a timeout, always written from the timeout
# path — including when the RETRY is the attempt that timed out. The report's
# `timeouts == 0` rehearsal gate finds this string and nothing else, so a timeout that
def finalize_blue_narratives(args):
    """Write a blue narrative for every team that ran, whatever its cycles did. Never raises.

    opencode is asked to write REPORT.md, and it only does so when a cycle ends with judgement to
    record: a timed-out, crashed or never-started cycle leaves nothing behind, so the artifact set
    got a stub `BLUE-TEAM.md` for a blue side that really did run (logged every run as
    "BLUE-TEAM.md is ABSENT although this run had a blue agent"). The narrative below is the
    harness's own record — the cycle feed's rc= lines, blue's LOG.md, the scoreboard delta — and it
    says so in the first line, so nobody files it as blue's own account. An existing substantive
    REPORT.md is never touched.
    """
    run_dir = Path(getattr(args, "run_dir", None) or ".")
    for n in range(1, (getattr(args, "teams", 0) or 0) + 1):
        wd = run_dir / f"blue-team{n}"
        if not wd.is_dir():
            continue
        report = wd / "REPORT.md"
        if report.exists() and report.stat().st_size > 1024:
            continue
        lines = [f"# Blue team {n} — after-action narrative (harness-synthesized)",
                 "",
                 "opencode did not write REPORT.md for this team, so the harness filed this "
                 "reconstruction",
                 "from its own records. It is NOT blue's own account.",
                 ""]
        feed = wd / "feed.log"
        if feed.exists():
            cycles = [ln.strip() for ln in feed.read_text().splitlines()
                      if ln.startswith("===== cycle")]
            events = [ln.strip() for ln in feed.read_text().splitlines()
                      if ln.startswith("===== ") and not ln.startswith("===== cycle")]
            lines += [f"## Cycles ({len(cycles)} recorded)", ""]
            lines += [f"- {c}" for c in cycles] or ["- none: no cycle completed"]
            if events:
                lines += ["", "## Other feed events", ""] + [f"- {e}" for e in events]
        else:
            lines += ["## Cycles", "", "- no feed.log: the blue loop never started"]
        log_md = wd / "LOG.md"
        if log_md.exists():
            body = [ln for ln in log_md.read_text().splitlines()
                    if not ln.startswith("# Blue team") and ln.strip()]
            if body:
                lines += ["", "## blue LOG.md (verbatim, last 40 lines)", ""] + body[-40:]
        try:
            delta = blue_prompt.scoreboard_delta(args.run_dir, f"team{n}")
        except Exception:
            delta = "(scoreboard delta unavailable)"
        if delta:
            lines += ["", "## Scored services at the end of the event", "", str(delta)]
        body = "\n".join(lines) + "\n"
        try:
            core.write_evidence(report, body)
            log(f"blue-team{n}: REPORT.md synthesized by the harness ({len(body)} bytes)")
        except OSError as e:
            log(f"WARNING: could not write blue-team{n} REPORT.md: {e}")


# the `timeouts == 0` rehearsal gate finds this string and nothing else, so a timeout that
# skips it passes the gate silently.
def write_cycle_timeout(wd, elapsed):
    with (Path(wd) / "feed.log").open("a") as f:
        f.write(f"\n===== cycle T+{elapsed} TIMEOUT =====\n")


# one lock acquisition per attempt: the lock used to wrap the attempt AND its retry, so
# two hung attempts held it for up to 2 x CYCLE_TIMEOUT and starved every other team on
# the shared endpoint of its whole cycle. Releasing between attempts lets a waiter run
# while this team is between attempts; the concurrency cap is unchanged. The retry is
# skipped once stop is set, so teardown is never held up by a doomed second attempt.
def run_cycle_with_retry(args, prompt, wd, env, llm_lock, stop, team=None):
    who = team if team is not None else "?"
    with llm_lock:
        r = _opencode_run(args, prompt, wd, env, stop=stop)
    if r.returncode != 0 and not stop.is_set():
        log(f"blue-team{who} cycle rc={r.returncode} — retrying once (lock released)")
        with llm_lock:
            r = _opencode_run(args, prompt, wd, env, stop=stop)
    return r
