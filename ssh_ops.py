"""SSH helpers via the scoring-engine gateway and Terraform context."""

import json
import os
import subprocess
import time
from pathlib import Path

import requests

from range_ops import diagnose_unreachable_box, wait_for_guest_agent


def is_windows_template(template_name):
    return "win" in template_name.lower()


def read_terraform_ctx():
    raw = subprocess.run(
        ["terraform", "output", "-json"], cwd="terraform", capture_output=True, text=True, check=True
    ).stdout
    ctx = json.loads(json.loads(raw)["agent_context"]["value"])
    key_path = ctx["ssh_key_path"]
    if not os.path.isabs(key_path):
        ctx = {**ctx, "ssh_key_path": str((Path("terraform") / key_path).resolve())}
    return ctx


def ssh_via_gateway(ctx, target_ip, cmd, timeout=60, user="ubuntu"):
    """SSH to a target box through the engine gateway (ProxyCommand -W)."""
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    proxy = (
        f"ssh -i {key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-W %h:%p {scoring_user}@{scoring_ip}"
    )
    return subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-o", f"ProxyCommand={proxy}",
            f"{user}@{target_ip}", cmd,
        ],
        capture_output=True, text=True, timeout=timeout,
    )


def ssh_on_gateway(ctx, cmd, timeout=30):
    """Run a command directly on the scoring engine gateway."""
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    return subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            f"{scoring_user}@{scoring_ip}", cmd,
        ],
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
                ["ssh", "-i", key,
                 "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "ConnectTimeout=10",
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
    deadline = time.time() + timeout
    total = 0
    unreachable = 0
    for t in targets:
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
