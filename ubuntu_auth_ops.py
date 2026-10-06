"""Password auth + NOPASSWD sudo setup so nakon can authenticate to Ubuntu boxes."""

import os
import re
import shlex
import subprocess
import time

from range_ops import guest_agent_exec_detached
from ssh_ops import gateway_proxy
from utils import PRINT_LOCK, run_concurrent, valid_unix_username


def setup_ubuntu_auth(targets, ctx):
    """Enable password auth + NOPASSWD sudo for box_username (nakon uses password auth + sudo).

    Raises when a box ends the pass without working auth: nakon authenticates
    with these credentials, so marching into the plant against a box that
    provably can't SSH just defers the failure into noisy per-config timeouts."""

    key = ctx["ssh_key_path"]
    box_username = ctx.get("box_username", "ubuntu")
    if not valid_unix_username(box_username):
        raise RuntimeError(
            f"box_username {box_username!r} is not a safe sudoers filename/remote-shell token"
        )
    proxy = gateway_proxy(ctx)

    print(f"  Enabling password auth + NOPASSWD sudo for {box_username} on team boxes...")
    sudoers_line = shlex.quote(f"{box_username} ALL=(ALL) NOPASSWD:ALL")
    # sshd_config.d drop-in: cloud-init ships `PasswordAuthentication no` in
    # /etc/ssh/sshd_config.d/50-cloud-init.conf (alpine; distro-matrix-2026-09-27), and
    # OpenSSH keeps the FIRST value seen — the Include beats the main-file sed lines
    # below. A 00- drop-in wins on every distro with the include; the sed lines still
    # cover distros whose sshd_config has no include.
    auth_cmd = (
        "sudo sed -i 's/^#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
        "sudo sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
        "sudo mkdir -p /etc/ssh/sshd_config.d; "
        "printf 'PasswordAuthentication yes\\n' | sudo tee /etc/ssh/sshd_config.d/00-tz-password-auth.conf >/dev/null; "
        "sudo systemctl restart sshd 2>/dev/null || true; "
        f"echo {sudoers_line} | sudo tee /etc/sudoers.d/{box_username}; "
        f"sudo chmod 440 /etc/sudoers.d/{box_username}"
    )

    def _auth(t):
        ip = t["ip"]

        def _via_agent(reason):
            with PRINT_LOCK:
                print(f"    Auth setup for {ip} fell back to guest agent (root): {reason}")
            vmid = t["vmid"]
            root_script = re.sub(r"\bsudo ", "", auth_cmd)
            try:
                res = guest_agent_exec_detached(
                    os.environ["TF_VAR_proxmox_node"], vmid, root_script,
                    f"/tmp/tz-auth-{vmid}.log", timeout=900)
                if res.rc == 0:
                    with PRINT_LOCK:
                        print(f"    Auth configured on {ip} (via guest agent)")
                    return True
                raise RuntimeError(
                    f"auth setup failed on {ip} via guest agent too: "
                    f"rc={res.rc} {res.log[-200:].strip()}")
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError(
                    f"auth setup failed on {ip}: SSH unusable ({reason}) and the "
                    f"guest-agent fallback raised ({e})") from e

        for attempt in range(1, 9):
            try:
                r = subprocess.run(
                    [
                        "ssh", "-i", key,
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "ConnectTimeout=10",
                        "-o", f"ProxyCommand={proxy}",
                        f"{box_username}@{ip}", auth_cmd,
                    ],
                    capture_output=True, text=True, timeout=40,
                )
                if r.returncode == 0:
                    with PRINT_LOCK:
                        print(f"    Auth configured on {ip}")
                    return True
                err = r.stderr or ""
                # Definitive rejections can't heal by waiting: a rejected key/password
                # or a dead gateway leg stays rejected on every retry. Straight to the
                # agent fallback instead of burning 8x15s (live: cyberrange loadtest).
                if ("Permission denied" in err or "REMOTE HOST IDENTIFICATION" in err
                        or "Host key verification failed" in err):
                    return _via_agent(err.strip().splitlines()[-1][:120]
                                      if err.strip() else "rejected")
            except subprocess.TimeoutExpired:
                pass  # box still booting — retry
            if attempt < 8:
                with PRINT_LOCK:
                    print(f"    Auth attempt {attempt}/8 failed for {ip}, retrying in 15s...")
                time.sleep(15)
        return _via_agent("unreachable after 8 attempts")

    results = run_concurrent(targets, _auth)
    for t, r in zip(targets, results):
        if r is True:
            continue
        if isinstance(r, Exception):
            raise r
        raise RuntimeError(f"auth setup failed on {t['ip']} after 8 attempts and the fallback")
