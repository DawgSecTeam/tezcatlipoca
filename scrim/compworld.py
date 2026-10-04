from scrim import core


def _web01_units(comp):
    """(candidate systemd units, scored port) for the fire test's stop/restore on web01.

    Returns every unit the web01 pins map to — which one actually exists is resolved
    on the box (apache2 vs httpd across distros) by the caller. The old code
    hardcoded nginx, which doesn't exist on comps whose web box runs apache
    (cde-2026: Fedora httpd) — the fire test would stop a non-existent unit, see no
    scoreboard change, and abort.

    scrims-one 2026-10-03: web01 pins ["ssh", "nginx"] — a port-less ssh pin took the
    `or 80` default, tied with nginx on the sort, and won by order, so the test
    stopped sshd while probing HTTP :80; the scoreboard never moved (nginx was still
    serving) and the run aborted pre-T0. So: a pin is httpish from its display, an
    explicit :80, or a well-known web NAME; a non-http pin gets no default port; the
    ssh unit is only ever a last resort — stopping it severs the operator's own path
    to the box and proves nothing about the scored web service."""
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
    # httpish first, then anything-but-sshd, then lowest port
    candidates.sort(key=lambda c: (not c[0], c[1], c[2]))
    return candidates[0][3], candidates[0][2], candidates[0][4]


def comp_world(comp):
    """Box/scenario facts for the blue cycle prompt, from the comp's own files.

    The prompt used to hardcode the Meridian 5-box world (box names, '8 scored
    services', nginx restore example, wardops ROE) and lied on every other lineup."""
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


def _verify_flags(comp):
    """Packet/no-vuln flags for verify-competition, derived from the comp's files.

    A packet-sourced comp gets the packet fidelity gates; an empty box_vulns.json
    means the misconfig spot-check has nothing to confirm and must be skipped
    (cde-2026 failed verify as 'FAIL' with every summary line PASS — the packet
    AD-misconfig model plants no box configurations)."""
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


# The scoreboard service name a catalog pin registers as (<box>-<Display>): the fire
# test watches its own row, and "first row starting with web01" is web01-ssh on any
# comp whose web01 carries an ssh pin — scrim-one 2026-10-03 "detected down" from a
# stale ssh outage and then failed to restore the nginx service it never watched.
_WATCHDOG_DISPLAY = {
    "nginx": "http", "apache": "http", "httpd": "http", "iis": "http",
    "bind": "dns", "named": "dns", "mysql": "sql", "mariadb": "sql", "mysqld": "sql",
    "postfix": "smtp", "exim4": "smtp", "exim": "smtp", "sendmail": "smtp",
    "dovecot": "imap", "vsftpd": "ftp", "ssh": "ssh", "openssh": "ssh", "sshd": "ssh",
    "splunk": "splunk",
}


def verify_cmd(comp, creds, *extra):
    """verify-competition.py argv against this comp's engine; `extra` sits before the derived flags."""
    return ["python3", "verify-competition.py", str(comp.relative_to(core.REPO)),
            "--engine-ip", creds["ENGINE_IP"], "--admin-password", creds["ADMIN_PW"],
            *extra, *_verify_flags(comp)]
