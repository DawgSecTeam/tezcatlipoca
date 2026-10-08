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


def _pull_red_files(args, files, tag=None):
    """Best-effort scp of red01 files — direct first, then via the engine jump.

    Never raises: in-run snapshots and teardown both use this, and a flaky scp must
    not take down a run that is otherwise fine.
    """
    ev = Path(args.run_dir) / "evidence" / "red"
    ev.mkdir(parents=True, exist_ok=True)
    target, common, jump = _red_ssh_ctx(args)
    got = []
    for remote, local in files:
        dest = ev / local
        for extra in ([], (["-o", jump] if jump else [])):
            try:
                r = procs.run_tree(["scp"] + common + extra + [f"{target}:{remote}", str(dest)],
                                   timeout=75, check=False)
            except subprocess.TimeoutExpired:
                continue
            if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                core.secure_evidence(dest)
                got.append(local)
                break
            dest.unlink(missing_ok=True)
    return got


def pull_red_snapshot(args, tag=None):
    """Best-effort in-run events.jsonl pull from red01. Never raises."""
    got = _pull_red_files(args, [("/var/lib/bad-auto/events.jsonl", "events.jsonl")])
    if got and tag:
        ev = Path(args.run_dir) / "evidence" / "red"
        core.secure_evidence(core.shutil_copy(ev / "events.jsonl", ev / f"events-{tag}.jsonl"))
    return bool(got)


def _staged_report_names(args):
    """Basenames of the report-*.md copies already staged under /tmp/ba (best effort)."""
    target, common, jump = _red_ssh_ctx(args)
    cmd = "ls -1 /tmp/ba 2>/dev/null | grep '^report-' || true"
    for extra in ([], (["-o", jump] if jump else [])):
        try:
            r = procs.run_tree(["ssh"] + common + extra +
                               ["-o", "ConnectTimeout=10", target, cmd], timeout=45, check=False)
        except subprocess.TimeoutExpired:
            continue
        names = [n for n in (r.stdout or "").split() if n.startswith("report-")]
        if names:
            return names
    return []


def pull_red_state(args):
    """Final pull of what red actually planted, BEFORE `badauto destroy` erases red01.

    world.json is the only record of the artifact set the persistence verification
    works from, and both it and events.jsonl are root-owned 0600 inside
    /var/lib/bad-auto — so they are staged to a readable path with sudo first. The
    artifact collector treats red01 as ONE target and marks every later file
    "skipped: target unreachable" after the first scp failure (RED-TEAM.md is a
    root-owned pattern match that fails when report-secrets.md is 0600), which is
    how a whole run's world.json went missing.
    """
    target, common, jump = _red_ssh_ctx(args)
    # report-secrets.md (the organizer-only credlist restore table) is 0600 root, so
    # the collector's scp as the box user fails on it and marks the whole red target
    # unreachable — losing its siblings too. Stage every root-owned artifact to a
    # world-readable dir first; the collector reads the staged copies.
    stage = ("sudo -n mkdir -p /tmp/ba && sudo -n chmod 755 /tmp/ba; "
             "sudo -n cp /var/lib/bad-auto/world.json /tmp/ba/world.json && "
             "sudo -n cp /var/lib/bad-auto/events.jsonl /tmp/ba/events.jsonl && "
             "for f in /var/lib/bad-auto/report-*.md; do "
             "sudo -n cp \"$f\" /tmp/ba/ 2>/dev/null; done; "
             "sudo -n chmod 644 /tmp/ba/* 2>/dev/null; true")
    for extra in ([], (["-o", jump] if jump else [])):
        procs.run_tree(["ssh"] + common + extra + ["-o", "ConnectTimeout=10", target, stage],
                       timeout=75, check=False)
    got = _pull_red_files(args, [
        ("/tmp/ba/world.json", "world.json"),
        ("/tmp/ba/events.jsonl", "events.jsonl"),
        ("/tmp/ba/report-secrets.md", "report-secrets.md"),
    ] + [(f"/tmp/ba/{name}", name) for name in _staged_report_names(args)])
    log(f"red state pulled before teardown: {got or 'nothing (see collection.json)'}")
    return got


def start_red_director(args):
    """Start red's director AFTER the day-0 seed has landed.

    `badauto deploy --start` boots the director before the seed runs: the director
    loads an EMPTY world.json, and its next save then overwrites the seed's artifact
    record (lost update — meta.seed_done gone, beacon_score reading single digits,
    per-channel re-plant blind to the seeded set). Starting it here fixes that and
    lines red's event clock up with T0.
    """
    target, common, jump = _red_ssh_ctx(args)
    cmd = ("sudo -n systemctl start bad-auto.service; sleep 3; "
           "systemctl is-active bad-auto.service")
    for extra in ([], (["-o", jump] if jump else [])):
        try:
            r = procs.run_tree(["ssh"] + common + extra + ["-o", "ConnectTimeout=10", target, cmd],
                               timeout=180, check=False)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0:
            log(f"red director started after the seed — {target} "
                f"{' '.join((r.stdout or '').split())[-24:]}")
            return True
    raise RuntimeError("could not start bad-auto.service on red01 after the seed")


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
