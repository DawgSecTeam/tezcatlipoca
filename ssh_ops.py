"""SSH helpers via the scoring-engine gateway and Terraform context."""

import json
import os
import subprocess
import time
from pathlib import Path

import requests

from range_ops import diagnose_unreachable_box, terraform_dir, wait_for_guest_agent

DEFAULT_KNOWN_HOSTS = str(Path.home() / ".tezcatlipoca" / "known_hosts")


def _engine_opts(known_hosts=None):
    """SSH -o options that authenticate the scoring engine: TOFU via a persistent known_hosts.

    accept-new pins the key on first connect and rejects a *changed* key afterwards (MITM
    protection); the residual exposure is the very first connection only. The keepalives
    matter for long quiet steps (a plant can hold the channel idle well past NAT conntrack
    timeouts — without them the operator-side ssh dies unnoticed and the driver blocks on
    a dead read while the remote side finishes alone)."""
    kh = known_hosts or DEFAULT_KNOWN_HOSTS
    Path(kh).parent.mkdir(parents=True, exist_ok=True)
    return ["-o", f"UserKnownHostsFile={kh}", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=6"]


def engine_ssh_opts(ctx):
    return _engine_opts(ctx.get("known_hosts"))


def gateway_proxy(ctx):
    """ProxyCommand that jumps through the engine. The engine hop is host-key pinned; the inner
    box hop stays unverified (box keys rotate on clone/rebuild/rollback) but is tunneled inside
    the now-authenticated engine channel."""
    opts = " ".join(engine_ssh_opts(ctx))
    return f"ssh -i {ctx['ssh_key_path']} {opts} -W %h:%p {ctx['vm_username']}@{ctx['scoring_engine_ip']}"


def forget_engine_host_key(ip, known_hosts=None):
    """Drop any pinned key for the engine IP so a freshly-rebuilt engine re-pins cleanly."""
    kh = known_hosts or DEFAULT_KNOWN_HOSTS
    if Path(kh).exists():
        subprocess.run(["ssh-keygen", "-R", ip, "-f", kh], capture_output=True, text=True)


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
    return subprocess.run(
        [
            "ssh", "-i", ctx["ssh_key_path"],
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-o", f"ProxyCommand={gateway_proxy(ctx)}",
            f"{user}@{target_ip}", cmd,
        ],
        capture_output=True, text=True, timeout=timeout,
    )


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
                ["ssh", "-i", key, *_engine_opts(),
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
    """Poll every target's reachability through the gateway; raises only if all boxes fail."""
    node = os.environ["TF_VAR_proxmox_node"]
    print("  Waiting for team boxes to accept SSH via gateway...")
    total = 0
    unreachable = 0
    for t in targets:
        # per-target budget: a shared deadline let two slow post-rollback Windows
        # boots burn the whole wait and starve the (healthy) Linux probes
        deadline = time.time() + timeout
        total += 1
        ip = t["ip"]
        windows = is_windows_template(t["box"]["template"])
        while True:
            if time.time() > deadline:
                print(f"    WARNING: {ip} not reachable within timeout — continuing")
                print(diagnose_unreachable_box(node, t["vmid"]))
                unreachable += 1
                break
            try:
                if windows:
                    ok = wait_for_guest_agent(node, t["vmid"], timeout=20)
                else:
                    ok = ssh_via_gateway(ctx, ip, "true", timeout=20,
                                         user=ctx.get("box_username", "ubuntu")).returncode == 0
                if ok:
                    print(f"    {ip} reachable")
                    break
            except Exception:
                pass
            time.sleep(10)

    if total > 0 and unreachable == total:
        raise RuntimeError(
            f"All {total} team box(es) failed to become SSH-reachable — this looks systemic "
            f"(see the guest-agent diagnosis above for each box), not a one-off timing fluke. "
            f"Aborting rather than burning through the DNS/auth/nakon retry loops for boxes "
            f"that are already known unreachable."
        )


def wait_for_cloud_init(ctx, targets, timeout=240):
    """Wait for cloud-init to finish on all targets before nakon plants anything."""
    print("  Waiting for cloud-init to finish on all team boxes...")
    deadline = time.time() + timeout
    for t in targets:
        if is_windows_template(t["box"]["template"]):
            continue
        ip = t["ip"]
        remaining = max(int(deadline - time.time()), 15)
        try:
            r = ssh_via_gateway(ctx, ip, "cloud-init status --wait", timeout=remaining,
                                user=ctx.get("box_username", "ubuntu"))
            if r.returncode in (0, 2):
                print(f"    {ip}: cloud-init done (rc={r.returncode})")
            else:
                print(f"  WARNING: {ip} cloud-init status --wait exited {r.returncode}: "
                      f"{(r.stdout or '').strip()[:150]}")
        except subprocess.TimeoutExpired:
            print(f"  WARNING: {ip} cloud-init still running after {remaining}s — continuing anyway")
        except Exception as e:
            print(f"  WARNING: cloud-init wait failed for {ip}: {e}")


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
