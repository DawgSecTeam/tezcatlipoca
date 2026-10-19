from pathlib import Path

from scrim import core


# The ssh unit is a last resort — stopping it severs the operator's own path to the box —
# and a non-http pin gets no default port so it cannot take :80 and outrank web.
def _web01_units(comp):
    pins = core.read_comp_json(comp, "box_services.json").get("web01", [])
    candidates = []
    for pin in pins:
        if isinstance(pin, str):
            pin = {"name": pin}
        name = pin.get("name", "")
        mapped = _WATCHDOG_UNITS.get(name, [])
        if not mapped:
            continue
        httpish = (pin.get("port") == 80 or pin.get("display") in ("http", "https")
                   or name in ("nginx", "apache", "httpd", "iis"))
        port = pin.get("port") or (80 if httpish else None)
        if port is None:
            continue
        display = pin.get("display") or _WATCHDOG_DISPLAY.get(name) or name
        candidates.append((httpish, name in ("ssh", "openssh", "sshd"), port, mapped,
                           display))
    if not candidates:
        raise SystemExit("  ERROR: no serviceable web01 unit for the fire test — "
                         "web01 pins in box_services.json map to nothing in _WATCHDOG_UNITS")
    candidates.sort(key=lambda c: (not c[0], c[1], c[2]))
    return candidates[0][3], candidates[0][2], candidates[0][4]


def comp_world(comp):
    boxes = core.read_comp_json(comp, "boxes.json")
    windows = [b["name"] for b in boxes if core.is_windows_box(b)]
    linux = [b["name"] for b in boxes if b["name"] not in windows]
    meta = {}
    for line in (comp / "Compfile").read_text().splitlines():
        key, _, value = line.partition(" ")
        if key in ("name", "scenario"):
            meta[key] = value.strip()
    try:
        units, _port, _display = _web01_units(comp)
    except SystemExit:
        units = None
    return {"name": meta.get("name", ""), "scenario": meta.get("scenario", ""),
            "linux": linux, "windows": windows,
            "web_unit": units[0] if units else None}


# A no-vuln comp must get --expect-no-vulns or verify fails it with every summary line PASS.
def _verify_flags(comp):
    flags = []
    try:
        src = next((l.split(None, 1)[1].strip() for l in (comp / "Compfile").read_text().splitlines()
                    if l.startswith("packet_source")), None)
    except OSError:
        src = None
    if src:
        flags += ["--packet", src]
    try:
        vulns = core.read_comp_json(comp, "box_vulns.json")
        if not any(vulns.values()):
            flags.append("--expect-no-vulns")
    except (OSError, ValueError):
        pass
    return flags


# Scored service name (box_services.json) -> candidate systemd units, first existing wins.
_WATCHDOG_UNITS = {
    "nginx": ["nginx"], "apache": ["apache2", "httpd"], "httpd": ["httpd", "apache2"],
    "bind": ["named", "bind9"], "named": ["named", "bind9"],
    "mysql": ["mysql", "mariadb"], "mariadb": ["mariadb", "mysql"], "mysqld": ["mysql", "mariadb"],
    "postfix": ["postfix"], "dovecot": ["dovecot"], "vsftpd": ["vsftpd"],
    "ssh": ["ssh", "sshd"], "openssh": ["ssh", "sshd"], "sshd": ["ssh", "sshd"],
    "splunk": ["Splunkd"], "exim4": ["exim4"], "exim": ["exim4"], "sendmail": ["sendmail"],
}


# Scoreboard rows are <box>-<Display>: the fire test must watch its own unit's row;
# "first row starting with web01" is web01-ssh on any comp that has an ssh pin there.
_WATCHDOG_DISPLAY = {
    "nginx": "http", "apache": "http", "httpd": "http", "iis": "http",
    "bind": "dns", "named": "dns", "mysql": "sql", "mariadb": "sql", "mysqld": "sql",
    "postfix": "smtp", "exim4": "smtp", "exim": "smtp", "sendmail": "smtp",
    "dovecot": "imap", "vsftpd": "ftp", "ssh": "ssh", "openssh": "ssh", "sshd": "ssh",
    "splunk": "splunk",
}


def comp_arg(comp):
    """Relative when inside the repo; absolute otherwise — the dashboard workspace sits outside it."""
    try:
        return str(Path(comp).relative_to(core.REPO))
    except ValueError:
        return str(Path(comp))


def verify_cmd(comp, creds, *extra):
    return [core.local_python(), "verify-competition.py", comp_arg(comp),
            "--engine-ip", creds["ENGINE_IP"], "--admin-password", creds["ADMIN_PW"],
            *extra, *_verify_flags(comp)]
