import subprocess
import time
from pathlib import Path

from artifacts_lib import artifacts_ops
from artifacts_lib.constants import SIDES
from common.comp_paths import resolve_comp_dir
from scrim import core
from scrim import procs
from scrim.core import log


# A canonical document smaller than this is a stub (the collector writes one when it finds
# nothing), never a narrative: 524 bytes was the observed BLUE-TEAM.md stub size.
STUB_MAX_BYTES = 1024
# Bounded grace for red's OWN report to land after the event clock stops. red writes
# /var/lib/tlaloc/report.md when its loop sees the duration run out, which can be
# seconds after the harness stops its own clock — the pre-teardown pull used to race exactly
# that gap and record a present red as "unreachable", losing its account of the event every run.
RED_REPORT_GRACE_SEC = 90
RED_REPORT_POLL_SEC = 10


def _red_ssh_spec(args):
    """`(ssh spec, common scp/ssh args)` for red01 — the one place its address is resolved.

    Two callers, one construction: `_red_ssh_ctx` (the harness's own scp/ssh) and
    `record_red_agent` (the `ssh` record in test.json that the teardown-time collector
    dials). They must not be able to disagree — a collector pointing at a different address
    fails silently as `unreachable`. The address is THIS run's own: the competition's
    record in the state/placement documents, else the CLI arg — never the
    sibling checkout's shared config.yaml, which on concurrent sessions names
    whichever red01 staged last (that read used to overwrite --red-ip outright)."""
    from red import red01_ops
    comp = resolve_comp_dir(args.competition, getattr(args, "comp_dir", None))
    red_ip = (getattr(args, "red_ip", None)
              or red01_ops.recorded_identity(comp).get("ip") or "")
    if not red_ip:
        raise RuntimeError("red01's address is not recorded for this competition — "
                           "stage red first, or pass --red-ip")
    key = core.ssh_key_path()
    user = core.vm_username()
    engine = None
    try:
        engine = core.engine_ip_from(comp)
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


RED_STATE_DIR = "/var/lib/tlaloc"
RED_UNIT = "tlaloc.service"


def red_state_dir(args):
    return RED_STATE_DIR


def red_unit(args):
    return RED_UNIT


def red_artifact_names(args):
    """The root-owned artifacts to stage + pull from red01's state dir."""
    return ["world.json", "events.jsonl", "report-secrets.md", "report.md",
            "restore-table.json"]


def _red_report_names(args, base=None):
    """Basenames of red's `report-*.md` on red01, newest last (the name carries the UTC stamp).

    Best effort, never raises: an unreadable listing means "no report yet", not a failed pull.
    """
    base = base or red_state_dir(args)
    # The narrative is report.md; report-secrets.md is the organizer-only file,
    # never the narrative, so it is filtered out below.
    glob = "report*.md"
    target, common, jump = _red_ssh_ctx(args)
    cmd = f"ls -1 {base}/{glob} 2>/dev/null || true"
    for extra in ([], (["-o", jump] if jump else [])):
        try:
            r = procs.run_tree(["ssh"] + common + extra +
                               ["-o", "ConnectTimeout=10", target, cmd], timeout=45, check=False)
        except subprocess.TimeoutExpired:
            continue
        names = [n.rsplit("/", 1)[-1] for n in (r.stdout or "").split() if n.endswith(".md")]
        names = [n for n in names if n != "report-secrets.md"]
        if names:
            return sorted(names)
    return []


def pull_red_snapshot(args, tag=None):
    """Best-effort in-run pull from red01: events.jsonl AND red's own report. Never raises.

    The report was only ever fetched at teardown, so a run whose red01 died mid-event (or whose
    teardown-time pull raced red's own write) had no narrative at all. Same cadence as the
    events snapshot — the harness calls this every ~60 s.
    """
    files = [(f"{red_state_dir(args)}/events.jsonl", "events.jsonl")]
    for name in _red_report_names(args)[-1:]:      # newest only: the final narrative
        files.append((f"{red_state_dir(args)}/{name}", name))
    for name in red_artifact_names(args):
        if name.endswith(".md") or name == "restore-table.json":
            files.append((f"{red_state_dir(args)}/{name}", name))
    got = _pull_red_files(args, files)
    if got and tag:
        ev = Path(args.run_dir) / "evidence" / "red"
        for name in got:
            core.secure_evidence(core.shutil_copy(ev / name, ev / f"{Path(name).stem}-{tag}{Path(name).suffix}"))
    return bool(got)


def await_red_report(args, grace=RED_REPORT_GRACE_SEC, interval=RED_REPORT_POLL_SEC):
    """Wait, bounded, for red's after-action report to appear on red01. Never raises.

    red is an autonomous agent: it writes its report when IT sees the event end, and the harness
    stops the clock first. Polling the remote listing (the report is written directly into
    /var/lib/tlaloc as root, so this looks at the originals, not the staged copies) closes that
    race without turning teardown into a gate — an expired grace is logged and the run continues.
    """
    deadline = time.time() + max(0, grace)
    while True:
        if _red_report_names(args):
            return True
        if time.time() >= deadline:
            log(f"red report: none appeared within {grace}s of the event ending — "
                f"RED-TEAM.md will be recorded as absent (not silently stubbed)")
            return False
        time.sleep(interval)


def _land_canonical_red_report(args, got):
    """File the newest pulled report as the test folder's canonical RED-TEAM.md.

    The collector does this too, but only when its own scp-jump route succeeds; landing it here
    means a report the harness already holds cannot be lost to a collector-route failure. Never
    overwrites a substantive narrative.
    """
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        return None
    reports = [Path(args.run_dir) / "evidence" / "red" / n for n in got
               if n == "report.md" or n.startswith("report-")]
    reports = [p for p in reports if p.is_file() and p.stat().st_size > 0]
    if not reports:
        return None
    dest = Path(test_dir) / SIDES["red"]
    if dest.exists() and dest.stat().st_size > STUB_MAX_BYTES:
        return None
    newest = max(reports, key=lambda p: (p.stat().st_mtime, p.name))
    try:
        facts = artifacts_ops.seal_local_file(newest, dest)
    except OSError as e:
        log(f"WARNING: could not file {SIDES['red']} into the test folder: {e}")
        return None
    log(f"{SIDES['red']} filed from {newest.name} ({facts.get('bytes')} bytes, "
        f"sha256 {str(facts.get('sha256'))[:12]})")
    return dest


def _staged_report_names(args):
    """Basenames of the report-*.md copies already staged under /tmp/ba (best effort)."""
    target, common, jump = _red_ssh_ctx(args)
    cmd = "ls -1 /tmp/ba 2>/dev/null | grep '^report' || true"
    for extra in ([], (["-o", jump] if jump else [])):
        try:
            r = procs.run_tree(["ssh"] + common + extra +
                               ["-o", "ConnectTimeout=10", target, cmd], timeout=45, check=False)
        except subprocess.TimeoutExpired:
            continue
        names = [n for n in (r.stdout or "").split()
                 if n.startswith("report") and "secrets" not in n]
        if names:
            return names
    return []


def pull_red_state(args):
    """Final pull of what red actually planted, BEFORE red01 is destroyed.

    world.json is the only record of the artifact set the persistence verification
    works from, and both it and events.jsonl are root-owned 0600 inside
    /var/lib/tlaloc — so they are staged to a readable path with sudo first. The
    artifact collector treats red01 as ONE target and marks every later file
    "skipped: target unreachable" after the first scp failure (RED-TEAM.md is a
    root-owned pattern match that fails when report-secrets.md is 0600), which is
    how a whole run's world.json went missing.
    """
    target, common, jump = _red_ssh_ctx(args)
    # red's narrative is written at the END of the event, seconds after the harness clock stops:
    # wait (bounded) for it before the staging copy below, or the pull races it and RED-TEAM.md
    # is recorded absent for an agent that was present and did report.
    await_red_report(args)
    # report-secrets.md (the organizer-only credlist restore table) is 0600 root, so
    # the collector's scp as the box user fails on it and marks the whole red target
    # unreachable — losing its siblings too. Stage every root-owned artifact to a
    # world-readable dir first; the collector reads the staged copies.
    stage = ("sudo -n mkdir -p /tmp/ba && sudo -n chmod 755 /tmp/ba; " +
             "".join(f"sudo -n cp {red_state_dir(args)}/{name} /tmp/ba/{name} "
                     f"2>/dev/null; " for name in red_artifact_names(args)) +
             f"for f in {red_state_dir(args)}/report*.md; do "
             "sudo -n cp \"$f\" /tmp/ba/ 2>/dev/null; done; "
             "sudo -n chmod 644 /tmp/ba/* 2>/dev/null; true")
    for extra in ([], (["-o", jump] if jump else [])):
        procs.run_tree(["ssh"] + common + extra + ["-o", "ConnectTimeout=10", target, stage],
                       timeout=75, check=False)
    staged = _staged_report_names(args)
    if not staged:
        log("red report: nothing staged under /tmp/ba — the `sudo -n cp` staging chain "
            "produced no report-*.md (check NOPASSWD on red01 for cp/chmod)")
    got = _pull_red_files(args, [
        ("/tmp/ba/world.json", "world.json"),
        ("/tmp/ba/events.jsonl", "events.jsonl"),
        ("/tmp/ba/report-secrets.md", "report-secrets.md"),
    ] + [(f"/tmp/ba/{name}", name) for name in red_artifact_names(args)
         if name not in ("world.json", "events.jsonl", "report-secrets.md")]
      + [(f"/tmp/ba/{name}", name) for name in staged])
    log(f"red state pulled before teardown: {got or 'nothing (see collection.json)'}")
    _land_canonical_red_report(args, got)
    return got


def start_red_director(args):
    """Start red's director AFTER the day-0 seed has landed.

    The agent must not boot its loop before the seed runs: the director
    loads an EMPTY world.json, and its next save then overwrites the seed's artifact
    record (lost update — meta.seed_done gone, beacon_score reading single digits,
    per-channel re-plant blind to the seeded set). Starting it here fixes that and
    lines red's event clock up with T0.
    """
    target, common, jump = _red_ssh_ctx(args)
    unit = red_unit(args)
    # RESTART, not start: a re-stage pushes new code onto red01, and `systemctl
    # start` on an already-running unit is a no-op — three re-arms in a row on
    # 2026-10-18 kept the pre-fix loop alive and every "fix" looked ineffective.
    cmd = (f"sudo -n systemctl restart {unit}; sleep 3; "
           f"systemctl is-active {unit}")
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
    raise RuntimeError(f"could not start {red_unit(args)} on red01 after the seed")


def red_box_power_state(args):
    """red01's power state straight from the hypervisor — liveness that does NOT need the
    guest's network.

    Every other red01 signal (ssh probe, event snapshot, report pull) travels the same path that
    is itself under suspicion, so a dead guest and a dead ROUTE TO it look identical. Live
    caught live: red01 vanished at T+13 and stayed gone for the rest of a 45-minute
    event; six red gates and RED-TEAM.md died with it, and the run log could never say whether the
    box was down or the route to it was — the post-mortem had to reconstruct it from another
    session's deploy times. This asks Proxmox, which needs no guest networking at all.
    """
    vmid = getattr(args, "red_vmid", None)
    if not vmid:
        return None
    try:
        from infra.client import proxmox_api
        from estate import config
        node = config.active().proxmox.node if config.active().is_set("proxmox.node") else ""
        if node:
            data = (proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/status/current") or {}).get("data") or {}
            return f"vmid {vmid} is {data.get('status') or 'unknown'} (Proxmox)"
        resources = (proxmox_api("GET", "/cluster/resources", params={"type": "vm"}) or {}).get("data") or []
        for res in resources:
            if str(res.get("vmid")) == str(vmid):
                return (f"vmid {vmid} is {res.get('status') or 'unknown'} "
                        f"on node {res.get('node')} (Proxmox)")
        return f"vmid {vmid} is not in the cluster (Proxmox)"
    except Exception as e:                                   # noqa: BLE001
        return f"vmid {vmid} unreadable from Proxmox ({type(e).__name__})"


def red_liveness(args):
    """The out-of-band answer to "is red01 alive?", naming the signal that answered.

    Every guest-path signal (ssh probe, event snapshot, report pull) travels the route that may
    itself be dead, so it cannot distinguish "red is down" from "the path to red is down". The
    hypervisor state needs no guest networking, so it answers even when ssh cannot connect
    (caught live: 30 minutes of `No route to host` with no way to tell).
    """
    return red_box_power_state(args) or "no red vmid recorded — no out-of-band signal"


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
            # 401/403 mean the endpoint ANSWERED: the probe carries no key on purpose (a key
            # would have to be shipped into red01's shell for a check that only asks "can red
            # dial this?"). Requiring exactly 200 made a keyed public API look unreachable and
            # aborted the run pre-T0.
            if code in ("200", "401", "403"):
                log(f"red01 reached the LLM endpoint ({path}, HTTP {code} — unauthenticated probe)")
                return True
            # say WHAT is unreachable: the box or the path to it. The out-of-band state is
            # asked on EVERY failure — an ssh that never connected produces no code, and the
            # old `if code` guard skipped the signal on exactly that shape.
            log(f"red01 LLM probe ({path}) -> {code or (r.stderr or '').strip()[:80] or 'no answer'}"
                f" [{red_liveness(args)}]")
        except subprocess.TimeoutExpired:
            log(f"red01 LLM probe ({path}) timed out [{red_liveness(args)}]")
    return False


def red_llm_url(args):
    """The LLM base URL as red01 dials it (tunnel-local when a tunnel is up).

    args.red_tunnel holds the CLI string until stage_red overwrites it with the
    live RedTunnel object (local endpoints only) — a truthy string must not be
    mistaken for a tunnel (openrouter run crashed at the first monitor tick)."""
    tunnel = getattr(args, "red_tunnel", None)
    return tunnel.red_base_url() if hasattr(tunnel, "red_base_url") else args.llm_base_url
