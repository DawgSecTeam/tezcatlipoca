"""PAM/SSH support-bundle collection over the guest-agent channel.

The bundle exists for the planted-box restart trap (docs/known-issues.md): SSH dies at the PAM
account stage while the guest agent still answers, so the collector must never depend on SSH.
`collect_bundle` takes the exec runner as a parameter; `capture_pam_bundle` binds the real
guest-agent one. Callers print paths/counts only — section output (shadow hashes, auth logs)
never goes to stdout.
"""

import json
import os
import time
from pathlib import Path

from infra.guest_exec import guest_agent_exec_root

BUNDLE_SCHEMA = 1
DEFAULT_TIMEOUT_S = 30

# One shell snippet per section. A nonzero rc is recorded, not fatal: a missing auth.log or
# pam_tally2 on a newer distro is itself a diagnostic datum.
BUNDLE_COMMANDS = (
    ("pam.d", "for f in /etc/pam.d/*; do printf '\\n### %s\\n' \"$f\"; cat \"$f\"; done"),
    ("security", "for f in /etc/security/*; do printf '\\n### %s\\n' \"$f\"; cat \"$f\"; done"),
    ("nsswitch.conf", "cat /etc/nsswitch.conf"),
    ("sshd_config",
     "for f in /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*; do "
     "[ -f \"$f\" ] && { printf '\\n### %s\\n' \"$f\"; cat \"$f\"; }; done"),
    ("sshd-T", "sshd -T 2>&1"),
    ("passwd", "getent passwd"),
    ("shadow", "getent shadow"),
    ("account-modes",
     "stat -c '%a %U %G %n' /etc/passwd /etc/shadow /etc/group /etc/gshadow "
     "/etc/passwd- /etc/shadow- /etc/group- /etc/gshadow- /etc/security/opasswd 2>&1"),
    ("clock", "date -Is; timedatectl 2>&1; hwclock -r 2>&1 || true"),
    ("aging",
     "for u in $(getent passwd | cut -d: -f1); do printf '\\n### %s\\n' \"$u\"; "
     "chage -l \"$u\" 2>&1; passwd -S \"$u\" 2>&1; done"),
    ("shadow-aging", "cut -d: -f1,3,5,7,8 /etc/shadow 2>&1"),
    ("faillock",
     "faillock 2>&1; ls -la /var/run/faillock 2>&1; "
     "for f in /var/run/faillock/*; do [ -f \"$f\" ] && faillock --user \"$(basename \"$f\")\" "
     "2>&1; done; pam_tally2 -l 2>&1 || true"),
    ("ssh-journal", "journalctl -u ssh -u sshd -n 300 --no-pager 2>&1"),
    ("auth-log", "tail -n 300 /var/log/auth.log 2>&1"),
    ("logind",
     "loginctl list-sessions --no-pager 2>&1; "
     "systemctl status systemd-logind --no-pager -l 2>&1"),
    ("packages",
     "dpkg -l 'libpam*' 'openssh*' 2>&1; "
     "dpkg --verify libpam-modules libpam-runtime openssh-server 2>&1"),
)


def _write_private(path, text):
    with open(path, "w", encoding="utf-8", errors="backslashreplace") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _section_text(command, rc, out, err):
    lines = [f"$ {command}", ""]
    if out:
        lines += ["--- stdout ---", out.rstrip("\n"), ""]
    if err:
        lines += ["--- stderr ---", err.rstrip("\n"), ""]
    if rc:
        lines += [f"--- rc={rc} ---", ""]
    return "\n".join(lines)


def collect_bundle(run, out_dir, *, metadata=None, timeout=DEFAULT_TIMEOUT_S):
    """Run every section through `run(command, timeout)` and write a 0700/0600 bundle; a lost
    channel stops the remaining sections instead of paying its timeout once per section."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    entries = []
    lost = False
    for name, command in BUNDLE_COMMANDS:
        if lost:
            entries.append({"name": name, "command": command, "status": "skipped",
                            "rc": None, "bytes": 0, "path": None})
            continue
        filename = f"{name}.txt"
        try:
            rc, out_text, err_text = run(command, timeout)
        except Exception as e:                       # noqa: BLE001 - a lost agent is a result
            entries.append({"name": name, "command": command, "status": "error", "rc": None,
                            "bytes": 0, "path": None,
                            "error": f"{type(e).__name__}: {e}"})
            lost = True
            continue
        text = _section_text(command, rc, out_text or "", err_text or "")
        _write_private(out / filename, text)
        entries.append({"name": name, "command": command, "status": "ok", "rc": rc,
                        "bytes": len(text.encode("utf-8", "backslashreplace")),
                        "path": filename})
    manifest = {"schema": BUNDLE_SCHEMA,
                "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "metadata": metadata or {}, "entries": entries}
    _write_private(out / "bundle.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    captured = sum(1 for e in entries if e["status"] == "ok")
    return {"dir": str(out), "entries": entries, "captured": captured}


def capture_pam_bundle(node, vmid, out_dir, *, metadata=None, timeout=DEFAULT_TIMEOUT_S):
    """collect_bundle over the guest-agent exec channel, which works while SSH is dead."""
    def run(script, command_timeout):
        return guest_agent_exec_root(node, vmid, script, timeout=command_timeout)

    meta = {"source": "common/pam_bundle.py", "node": node, "vmid": int(vmid)}
    meta.update(metadata or {})
    return collect_bundle(run, out_dir, metadata=meta, timeout=timeout)
