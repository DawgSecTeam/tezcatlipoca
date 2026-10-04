import json
import subprocess
from pathlib import Path

from scrim import core
from scrim import procs
from scrim.core import log


def _red_ssh_spec(args):
    """`(ssh spec, common scp/ssh args)` for red01 — the one place its address is resolved.

    Two callers, one construction: `_red_ssh_ctx` (the harness's own scp/ssh) and
    `record_red_agent` (the `ssh` record in test.json that the teardown-time collector
    dials). They must not be able to disagree — a collector pointing at a different address
    fails silently as `unreachable`, and the manifest is the only per-run source of truth
    because bad-auto's config.yaml is a rewritten singleton."""
    red_ip = getattr(args, "red_ip", None) or "10.0.0.198"
    try:
        red_ip = json.loads((core.BAD_AUTO / "config.yaml").read_text())["deploy"]["red_ip"]
    except Exception:
        pass
    key = str(core.REPO / "proxmox")
    user = core.vm_username()
    engine = None
    try:
        engine = core.engine_ip_from(core.REPO / "competitions" / args.competition)
    except Exception:
        pass
    common = [*core.SSH_NOCHECK, "-o", "ConnectTimeout=15", "-i", key]
    jump = f"ProxyCommand={core.engine_proxy(key, user, engine)}" if engine else None
    return {"user": user, "host": red_ip, "key": key, "jump": jump}, common


def _red_ssh_ctx(args):
    """(target, common ssh/scp args, engine jump ProxyCommand or None) for red01."""
    spec, common = _red_ssh_spec(args)
    return f"{spec['user']}@{spec['host']}", common, spec["jump"]


def pull_red_snapshot(args, tag=None):
    """Best-effort in-run events.jsonl pull from red01. Never raises."""
    ev = Path(args.run_dir) / "evidence" / "red"
    ev.mkdir(parents=True, exist_ok=True)
    target, common, jump = _red_ssh_ctx(args)
    dest = ev / "events.jsonl"
    for extra in ([], (["-o", jump] if jump else [])):
        try:
            r = procs.run_tree(["scp"] + common + extra +
                         [f"{target}:/var/lib/bad-auto/events.jsonl", str(dest)],
                               timeout=75, check=False)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            core.secure_evidence(dest)
            if tag:
                core.secure_evidence(core.shutil_copy(dest, ev / f"events-{tag}.jsonl"))
            return True
        dest.unlink(missing_ok=True)
    return False


def check_red_llm(args, base_url):
    """From-red01 LLM reachability gate.

    validate-llm runs operator-side and proves nothing about what red01 can
    reach — the dress run went red-LLM-less the whole event on exactly that
    gap. Probes <base_url>/models from red01 itself (direct, then through the
    engine jump host)."""
    target, common, jump = _red_ssh_ctx(args)
    probe = f"curl -sm 10 -o /dev/null -w '%{{http_code}}' {base_url.rstrip('/')}/models"
    for path, extra in (("direct", []), ("engine jump", ["-o", jump] if jump else None)):
        if extra is None:
            continue
        try:
            r = procs.run_tree(["ssh"] + common + extra + [target, probe],
                               timeout=45, check=False)
            code = (r.stdout or "").strip()
            if code == "200":
                return True
            log(f"red01 LLM probe ({path}) -> {code or (r.stderr or '').strip()[:80] or 'no answer'}")
        except subprocess.TimeoutExpired:
            log(f"red01 LLM probe ({path}) timed out")
    return False


def red_llm_url(args):
    """The LLM base URL as red01 dials it (tunnel-local when a tunnel is up).

    args.red_tunnel holds the CLI string until stage_red overwrites it with the
    live RedTunnel object (local endpoints only) — a truthy string must not be
    mistaken for a tunnel (openrouter run crashed at the first monitor tick)."""
    tunnel = getattr(args, "red_tunnel", None)
    return tunnel.red_base_url() if hasattr(tunnel, "red_base_url") else args.llm_base_url
