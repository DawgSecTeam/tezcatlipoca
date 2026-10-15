"""Password auth + NOPASSWD sudo setup so nakon can authenticate to Ubuntu boxes."""

import re
import shlex

from infra.guest_exec import guest_agent_exec_detached
from common.ssh_ops import box_ssh_retry
from common.utils import PRINT_LOCK, run_concurrent, valid_unix_username
from estate import config


def setup_ubuntu_auth(targets, ctx):
# Enable password auth + NOPASSWD sudo for box_username (nakon uses password auth + sudo).
#
#     Raises when a box ends the pass without working auth: nakon authenticates
#     with these credentials, so marching into the plant against a box that
#     provably can't SSH just defers the failure into noisy per-config timeouts.

    box_username = ctx.get("box_username", "ubuntu")
    if not valid_unix_username(box_username):
        raise RuntimeError(
            f"box_username {box_username!r} is not a safe sudoers filename/remote-shell token"
        )
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
                    config.active().proxmox.node, vmid, root_script,
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

        ok, rejection = box_ssh_retry(ctx, ip, box_username, auth_cmd,
                                      timeout=40, attempts=8, what="Auth")
        if ok:
            with PRINT_LOCK:
                print(f"    Auth configured on {ip}")
            return True
        if rejection:
            return _via_agent(rejection.strip().splitlines()[-1][:120])
        return _via_agent("unreachable after 8 attempts")

    results = run_concurrent(targets, _auth)
    for t, r in zip(targets, results):
        if r is True:
            continue
        if isinstance(r, Exception):
            raise r
        raise RuntimeError(f"auth setup failed on {t['ip']} after 8 attempts and the fallback")
