#!/usr/bin/env python3
"""Realistic agent-cycle bench on the PRODUCTION code path.

Reproduces blue_feed_loop's staging hygiene (per-cycle HOME/XDG, fresh service port,
opencode.jsonc from OPENCODE_PROJECT_CFG, minimal env) and runs cycles through
blue_agent._opencode_run so cap-killing, server reaping and partial logs are the real
thing. The workload is a blue-style triage drill over LOCAL files (no live range):
notebook-first, bounded changes, LOG line, REPORT summary — enough tool calls to
exercise multi-turn streaming, which is where run-84c0e45d's cycles died.

Phases:
  A  one serial cycle per model   — latency/completion baseline
  B  parallel waves               — gateway concurrency (1/team x3, then 2/team x3)
Results append to .night4/bench-results.json (one line per cycle).
"""
import json, os, re, socket, sys, threading, time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scrim import blue_agent, endpoints          # noqa: E402
from scrim.blue_helpers import OPENCODE_PROJECT_CFG  # noqa: E402

GATEWAY = "https://litellm.dawgsec.com/v1"
MODELS = ("k2", "qwen3.8-flash-next", "qwen3.8-27b")
CAP = int(os.environ.get("BENCH_CAP", "900"))
ROOT = Path(__file__).resolve().parent / "bench-wd"
KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "SHELL", "TMPDIR", "USER", "LOGNAME")

PROMPT = """You are Blue Team 1 running a triage drill. Everything is LOCAL in this directory \
— no network, no live boxes. Files: services.csv (name,box,status), incidents.log (the week's \
incident lines), NOTEBOOK.md, LOG.md.

CYCLE TASK — finish ALL of it and end your reply; do not explore beyond these files:
0. FIRST update NOTEBOOK.md: one SNAPSHOT line with current state + next action. Then act.
1. From services.csv, find every service whose status is DOWN; for each, grep incidents.log \
   for its name and note what killed it.
2. Restore EXACTLY ONE service — the OLDEST incident wins: write a file restore-<svcname>.txt \
   containing the simulated restore command and the evidence line that justifies it.
3. Append one timestamped line to LOG.md describing what you did.
4. Write REPORT.md: 5-10 lines — what is down, what you restored, what to check next cycle.
End your reply with the single word DONE when all four steps are on disk."""


def fixture(wd):
    names = ["web-http", "web-ssh", "db-sql", "db-ssh", "ftp-svc", "ftp-ssh", "ad-dns",
             "ad-ldap", "ad-smb", "mail-smtp", "mon-http", "nas-nfs"]
    (wd / "services.csv").write_text(
        "name,box,status\n" + "\n".join(
            f"{nm},box{i % 4 + 1},{'DOWN' if i % 3 == 0 else 'UP'}"
            for i, nm in enumerate(names)) + "\n")
    (wd / "incidents.log").write_text("\n".join(
        f"2026-11-{10 + i // 40:02d}T{i % 24:02d}:{i % 60:02d}Z box{i % 4 + 1} {names[i % 12]} "
        f"{'crashed' if i % 2 else 'killed by beacon pid ' + str(1000 + i)}"
        for i in range(240)) + "\n")
    (wd / "NOTEBOOK.md").write_text("# Notebook\n(no entries yet)\n")
    (wd / "LOG.md").write_text("# Blue team 1 — defense log\n")


def stage(wd, model):
    home = wd / ".opencode-home"
    for sub in ("data", "config", "cache"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    svc = home / "config" / "opencode"
    svc.mkdir(parents=True, exist_ok=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        (svc / "service.json").write_text(json.dumps({"port": s.getsockname()[1]}))
    env = {k: os.environ[k] for k in KEEP if k in os.environ}
    env.update({"HOME": str(home), "XDG_DATA_HOME": str(home / "data"),
                "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache")})
    key_env = endpoints.key_env_name(GATEWAY) or "OPENROUTER_API_KEY"
    env[key_env] = endpoints.api_key(GATEWAY)
    (wd / "opencode.jsonc").write_text(
        OPENCODE_PROJECT_CFG.replace("{PROVIDER_KEY}", endpoints.provider_key(GATEWAY))
        .replace("{BASE_URL}", GATEWAY)
        .replace("{API_KEY_FIELD}", "{env:%s}" % key_env)
        .replace("{MODEL_ID}", model)
        .replace("{CTX}", "120000").replace("{OUT}", "16000").replace("{EFFORT}", ""))
    fixture(wd)
    return env


def run_cycle(tag, model):
    wd = ROOT / f"{tag}__{model}"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "cycles").mkdir(exist_ok=True)
    env = stage(wd, model)
    args = SimpleNamespace(cycle_timeout=CAP, blue_base_url=GATEWAY, blue_model=model,
                           duration_min=999, reasoning_effort=None)
    t = time.time()
    rec = {"tag": tag, "model": model, "cap": CAP, "t0": t}
    try:
        r = blue_agent._opencode_run(args, PROMPT, wd, env, ep=(GATEWAY, model))
        out = re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout or "") + (r.stderr or ""))
        rec.update(rc=r.returncode, secs=round(time.time() - t, 1),
                   done="DONE" in out, report=(wd / "REPORT.md").exists(),
                   err=("" if r.returncode == 0 else out[-300:]))
    except Exception as e:                                    # CycleTimeout lands here
        rec.update(rc="timeout", secs=round(time.time() - t, 1), done=False,
                   report=(wd / "REPORT.md").exists(), err=str(e)[:200])
    (wd / "result.json").write_text(json.dumps(rec, indent=1))
    with (ROOT.parent / "bench-results.json").open("a") as f:
        f.write(json.dumps(rec) + "\n")
    line = (f"{tag:12s} {model:20s} rc={str(rec['rc']):7s} {rec['secs']:6.1f}s "
            f"done={rec['done']} report={rec['report']} {rec['err'][:80]}")
    print(line, flush=True)
    return rec


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    print(f"=== Phase A: serial, one cycle per model (cap {CAP}s) ===", flush=True)
    for m in MODELS:
        run_cycle("serial", m)
    print("=== Phase B1: 3 parallel (one per model) ===", flush=True)
    with ThreadPool() as pool:
        for m in MODELS:
            pool.submit(run_cycle, "par1", m)
    print("=== Phase B2: 6 parallel (two per model) ===", flush=True)
    with ThreadPool(6) as pool:
        for m in MODELS:
            pool.submit(run_cycle, "par2a", m)
            pool.submit(run_cycle, "par2b", m)
    print("=== bench complete ===", flush=True)


class ThreadPool:
    def __init__(self, n=8):
        self.threads = []
        self.n = n
        self.sem = threading.Semaphore(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        for t in self.threads:
            t.join()

    def submit(self, fn, *a):
        def go():
            with self.sem:
                fn(*a)
        t = threading.Thread(target=go)
        self.threads.append(t)
        t.start()


if __name__ == "__main__":
    main()
