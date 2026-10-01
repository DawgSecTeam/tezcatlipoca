"""SSH helpers via the scoring-engine gateway and Terraform context."""

import json
import os
import subprocess
import time
from pathlib import Path

import requests

from range_ops import diagnose_unreachable_box, terraform_dir, wait_for_guest_agent
from utils import PRINT_LOCK, run_concurrent

DEFAULT_KNOWN_HOSTS = str(Path.home() / ".tezcatlipoca" / "known_hosts")


def _engine_opts(known_hosts=None, host=None):
    """SSH -o options that authenticate the scoring engine: TOFU via a persistent known_hosts.

    accept-new pins the key on first connect and rejects a *changed* key afterwards (MITM
    protection); the residual exposure is the very first connection only.

    With host, the options multiplex over one ControlMaster socket (M1.5): every per-box
    ProxyCommand and every direct engine call reuses a single authenticated engine
    connection instead of paying a fresh double handshake per call. engine_ops raises
    sshd's MaxSessions to 64 so M2's concurrent workers can share the master."""
    kh = known_hosts or DEFAULT_KNOWN_HOSTS
    Path(kh).parent.mkdir(parents=True, exist_ok=True)
    opts = ["-o", f"UserKnownHostsFile={kh}", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
            # Dead-peer detection: a silently dropped node link otherwise leaves an
            # established TCP session hanging until the caller's full timeout (live:
            # a docker build burned its whole 1800s budget against a dead node).
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
    if host:
        opts += ["-o", "ControlMaster=auto",
                 "-o", f"ControlPath={Path(kh).parent / f'cm-{host}-22-%r'}",
                 "-o", "ControlPersist=900"]
    return opts


def engine_ssh_opts(ctx):
    return _engine_opts(ctx.get("known_hosts"), host=ctx.get("scoring_engine_ip"))


def gateway_proxy(ctx):
    """ProxyCommand that jumps through the engine. The engine hop is host-key pinned; the inner
    box hop stays unverified (box keys rotate on clone/rebuild/rollback) but is tunneled inside
    the now-authenticated engine channel."""
    opts = " ".join(engine_ssh_opts(ctx))
    return f"ssh -i {ctx['ssh_key_path']} {opts} -W %h:%p {ctx['vm_username']}@{ctx['scoring_engine_ip']}"


def forget_engine_host_key(ip, known_hosts=None):
    """Drop any pinned key for the engine IP so a freshly-rebuilt engine re-pins cleanly,
    and retire the ControlMaster socket — a surviving master would keep speaking with the
    old host key and old session."""
    kh = known_hosts or DEFAULT_KNOWN_HOSTS
    if Path(kh).exists():
        subprocess.run(["ssh-keygen", "-R", ip, "-f", kh], capture_output=True, text=True)
    for sock in Path(kh).parent.glob(f"cm-{ip}-*"):
        try:
            subprocess.run(["ssh", "-o", f"ControlPath={sock}", "-O", "exit", f"x@{ip}"],
                           capture_output=True, timeout=5)
        except subprocess.SubprocessError:
            pass
        try:
            sock.unlink()
        except OSError:
            pass


def is_windows_template(template_name):
    return "win" in template_name.lower()


def read_terraform_ctx(comp_dir=None):
    """Read agent_context from `terraform output -json`. With comp_dir, read the
    competition's own per-comp state (competitions/<id>/terraform); without it,
    fall back to the legacy shared terraform/ dir."""
    tf_dir = str(terraform_dir(comp_dir)) if comp_dir else "terraform"
    raw = subprocess.run(
        ["terraform", "output", "-json"], cwd=tf_dir, capture_output=True, text=True, check=True
    ).stdout
    ctx = json.loads(json.loads(raw)["agent_context"]["value"])
    key_path = ctx["ssh_key_path"]
    if not os.path.isabs(key_path):
        ctx = {**ctx, "ssh_key_path": str((Path(tf_dir) / key_path).resolve())}
    return ctx


def ssh_via_gateway(ctx, target_ip, cmd, timeout=60, user="ubuntu"):
    """SSH to a target box through the engine gateway (ProxyCommand -W)."""
    args = [
        "ssh", "-i", ctx["ssh_key_path"],
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", f"ProxyCommand={gateway_proxy(ctx)}",
        f"{user}@{target_ip}", cmd,
    ]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode == 255 and "REMOTE HOST IDENTIFICATION HAS CHANGED" in (r.stderr or ""):
        # Live-found 2026-09-29 (cyberrange loadtest): a re-cloned engine served a new
        # host key on a re-run while the TOFU pin survived, hard-failing every proxied
        # call. Re-pin once and retry — the box hop inside the tunnel is unverified
        # anyway, so the pin guards a lab-internal hop, not an internet path.
        forget_engine_host_key(ctx["scoring_engine_ip"])
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r


def ssh_on_gateway(ctx, cmd, timeout=30):
    """Run a command directly on the scoring engine gateway."""
    return subprocess.run(
        ["ssh", "-i", ctx["ssh_key_path"], *engine_ssh_opts(ctx),
         f"{ctx['vm_username']}@{ctx['scoring_engine_ip']}", cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def ssh_to_engine(ctx, cmd, timeout=30):
    """Alias for ssh_on_gateway — SSH directly to the scoring engine itself."""
    return ssh_on_gateway(ctx, cmd, timeout=timeout)


def wait_for_ssh(key, user, host, timeout=300):
    """Poll SSH until `ssh user@host true` succeeds. Returns True/False; never raises."""

    print(f"  Waiting for {host} to accept SSH (timeout {timeout}s)...")
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            r = subprocess.run(
                ["ssh", "-i", key, *_engine_opts(host=host),
                 "-o", "BatchMode=yes",
                 f"{user}@{host}", "true"],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0:
                print(f"    {host} reachable via SSH (after {attempt} attempt(s))")
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(5)
    print(f"  WARNING: {host} not reachable via SSH within {timeout}s — continuing anyway")
    return False


def wait_for_boxes_ssh(ctx, targets, timeout=300):
    """Poll every target's reachability through the gateway, all boxes concurrently, each
    with its own full budget (a slow early box no longer eats later boxes' patience).
    Raises only if all boxes fail."""
    node = os.environ["TF_VAR_proxmox_node"]
    print("  Waiting for team boxes to accept SSH via gateway...")
    deadline = time.time() + timeout

    def _probe(t):
        ip = t["ip"]
        windows = is_windows_template(t["box"]["template"])
        while time.time() < deadline:
            try:
                if windows:
                    ok = wait_for_guest_agent(node, t["vmid"], timeout=20)
                else:
                    ok = ssh_via_gateway(ctx, ip, "true", timeout=20,
                                         user=ctx.get("box_username", "ubuntu")).returncode == 0
                if ok:
                    with PRINT_LOCK:
                        print(f"    {ip} reachable")
                    return True
            except Exception:
                pass
            time.sleep(10)
        with PRINT_LOCK:
            print(f"    WARNING: {ip} not reachable within timeout — continuing")
            print(diagnose_unreachable_box(node, t["vmid"]))
        return False

    results = run_concurrent(targets, _probe)
    if targets and all(r is not True for r in results):
        raise RuntimeError(
            f"All {len(targets)} team box(es) failed to become SSH-reachable — this looks systemic "
            f"(see the guest-agent diagnosis above for each box), not a one-off timing fluke. "
            f"Aborting rather than burning through the DNS/auth/nakon retry loops for boxes "
            f"that are already known unreachable."
        )


def wait_for_cloud_init(ctx, targets, timeout=240):
    """Wait for cloud-init to finish on all targets before nakon plants anything, concurrently:
    each box gets the full budget from its own thread."""
    print("  Waiting for cloud-init to finish on all team boxes...")
    deadline = time.time() + timeout

    def _wait(t):
        if is_windows_template(t["box"]["template"]):
            return "skipped"
        ip = t["ip"]
        remaining = max(int(deadline - time.time()), 15)
        try:
            r = ssh_via_gateway(ctx, ip, "cloud-init status --wait", timeout=remaining,
                                user=ctx.get("box_username", "ubuntu"))
            if r.returncode in (0, 2):
                with PRINT_LOCK:
                    print(f"    {ip}: cloud-init done (rc={r.returncode})")
                return "ok"
            with PRINT_LOCK:
                print(f"  WARNING: {ip} cloud-init status --wait exited {r.returncode}: "
                      f"{(r.stdout or '').strip()[:150]}")
            return f"rc={r.returncode}"
        except subprocess.TimeoutExpired:
            with PRINT_LOCK:
                print(f"  WARNING: {ip} cloud-init still running after {remaining}s — continuing anyway")
            return "timeout"
        except Exception as e:
            with PRINT_LOCK:
                print(f"  WARNING: cloud-init wait failed for {ip}: {e}")
            return "error"

    run_concurrent(targets, _wait)


def wait_for_http(url, timeout=120):
    """Poll URL until any HTTP response. Never raises; warns on timeout."""

    print(f"  Waiting for {url} to respond (timeout {timeout}s)...")
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            requests.get(url, timeout=5)
            print(f"    {url} responded (after {attempt} attempt(s))")
            return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(3)
    print(f"  WARNING: {url} did not respond within {timeout}s — continuing anyway")
    return False
