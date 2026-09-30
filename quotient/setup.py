"""Drives Quotient: build_event_conf(), seed_teams(), unpause_engine(), create_injects()."""

import time
from pathlib import Path

import requests

from constants import PIN_CHECK_OVERRIDES

# The event.conf box slices Quotient understands — also the valid `check` values for
# score-only pins (upstream has only these; docs/internals.md).
_CHECK_SLICES = frozenset({"Web", "Dns", "Ssh", "Ftp", "Smtp", "Imap", "Sql", "Tcp"})

_SERVICE_TO_CHECK = {
    "apache":    ("Web",  {"Display": "http",      "Port": 80,   "Scheme": "http", "Url": [{"Path": "/", "Status": 200}]}),
    "nginx":     ("Web",  {"Display": "http",      "Port": 80,   "Scheme": "http", "Url": [{"Path": "/", "Status": 200}]}),
    "httpd":     ("Web",  {"Display": "http",      "Port": 80,   "Scheme": "http", "Url": [{"Path": "/", "Status": 200}]}),
    "splunk":    ("Web",  {"Display": "splunk",    "Port": 8000, "Scheme": "http", "Url": [{"Path": "/", "Status": 200}]}),
    "roundcube": ("Web",  {"Display": "roundcube", "Port": 80,   "Scheme": "http", "Url": [{"Path": "/", "Status": 200}]}),
    "bind":      ("Dns",  {"Display": "dns",  "Port": 53, "Record": [{"Kind": "A", "Domain": "localhost", "Answer": ["127.0.0.1"]}]}),
    "named":     ("Dns",  {"Display": "dns",  "Port": 53, "Record": [{"Kind": "A", "Domain": "localhost", "Answer": ["127.0.0.1"]}]}),
    "ssh":       ("Ssh",  {"Display": "ssh",  "Port": 22,  "CredLists": ["linux.credlist"]}),
    "openssh":   ("Ssh",  {"Display": "ssh",  "Port": 22,  "CredLists": ["linux.credlist"]}),
    "sshd":      ("Ssh",  {"Display": "ssh",  "Port": 22,  "CredLists": ["linux.credlist"]}),
    "vsftpd":    ("Ftp",  {"Display": "ftp",  "Port": 21,  "CredLists": ["linux.credlist"]}),
    "ftpd":      ("Ftp",  {"Display": "ftp",  "Port": 21,  "CredLists": ["linux.credlist"]}),
    "ftp":       ("Ftp",  {"Display": "ftp",  "Port": 21,  "CredLists": ["linux.credlist"]}),
    "postfix":   ("Smtp", {"Display": "smtp", "Port": 25,  "CredLists": ["linux.credlist"]}),
    "sendmail":  ("Smtp", {"Display": "smtp", "Port": 25,  "CredLists": ["linux.credlist"]}),
    "exim":      ("Smtp", {"Display": "smtp", "Port": 25,  "CredLists": ["linux.credlist"]}),
    "exim4":     ("Smtp", {"Display": "smtp", "Port": 25,  "CredLists": ["linux.credlist"]}),
    "dovecot":   ("Imap", {"Display": "imap", "Port": 143, "CredLists": ["linux.credlist"]}),
    "cyrus":     ("Imap", {"Display": "imap", "Port": 143, "CredLists": ["linux.credlist"]}),
    "mariadb":   ("Sql",  {"Display": "sql",  "Port": 3306, "Kind": "mysql", "CredLists": ["linux.credlist"]}),
    "mysql":     ("Sql",  {"Display": "sql",  "Port": 3306, "Kind": "mysql", "CredLists": ["linux.credlist"]}),
    "mysqld":    ("Sql",  {"Display": "sql",  "Port": 3306, "Kind": "mysql", "CredLists": ["linux.credlist"]}),
    "telnet-service": ("Tcp", {"Display": "telnet", "Port": 23}),

    "Enable WinRM":   ("Tcp", {"Display": "winrm", "Port": 5985}),
    "New SMB Share":  ("Tcp", {"Display": "smb",   "Port": 445}),
    "RDP misconfigs": ("Tcp", {"Display": "rdp",   "Port": 3389}),
    "IIS HTTP":       ("Web", {"Display": "iis",   "Port": 80, "Scheme": "http", "Url": [{"Path": "/", "Status": 200}]}),
    "IIS FTP":        ("Ftp", {"Display": "ftp",   "Port": 21,  "CredLists": ["linux.credlist"]}),

    # Scored but not nakon-planted: domain_ops owns ADDS/Domain Join (phase 6 injects
    # them with per-team vars); generate_nakon_config strips them from machine lists.
    "ADDS":           ("Tcp", {"Display": "ldap",  "Port": 389}),

    # Score-only pins ({"name": "score/tcp", "score_only": true, "display": ..., "port":
    # ...}) never appear here — _resolve_pin builds their check directly. They score a
    # NATIVE service the catalog can't plant (AD's own DNS on 53, the AD SYSVOL share on
    # 445): no nakon plant, no catalog-check entry, a real scored check.
}


def _resolve_pin(pin):
    """(svc_name, check_key, check_cfg) for one box_services.json pin.

    Pins are a bare catalog name or {"name": ..., "vars": {...}} plus optional scoring
    overrides (PIN_CHECK_OVERRIDES); score-only pins ({"score_only": true, "check":
    "Tcp", "display": ..., "port": ...}) carry no plant at all. Unknown service names
    return check_key=None (the caller warns). check_cfg is a fresh dict whenever
    overrides apply."""
    svc_name = pin if isinstance(pin, str) else pin.get("name")
    if isinstance(pin, dict) and pin.get("plant_only"):
        # Mirror of score_only: the pin rides the machine list (a real plant) but
        # emits no scored check — the service is scored by a separate score-only pin
        # (e.g. IIS FTP plants the site, score/tcp:21 scores the port).
        return svc_name, None, None
    if isinstance(pin, dict) and pin.get("score_only"):
        check_key = pin.get("check") or "Tcp"
        if check_key not in _CHECK_SLICES:
            raise SystemExit(
                f"[event.conf] score-only pin {pin!r}: check must be one of "
                f"{sorted(_CHECK_SLICES)}")
        if not pin.get("port"):
            raise SystemExit(f"[event.conf] score-only pin {pin!r} must set a port")
        return svc_name, check_key, {"Display": pin.get("display") or "score",
                                     "Port": pin["port"]}
    check_key, base_cfg = _SERVICE_TO_CHECK.get(svc_name, (None, None))
    if check_key is None:
        return svc_name, None, None
    cfg = dict(base_cfg)
    if isinstance(pin, dict):
        for key in ("display", "port", "scheme"):
            if key in pin:
                cfg[{"display": "Display", "port": "Port", "scheme": "Scheme"}[key]] = pin[key]
        if "path" in pin or "status" in pin:
            if not cfg.get("Url"):
                raise SystemExit(
                    f"[event.conf] pin {pin!r} sets path/status but '{svc_name}' checks are not "
                    "URL-based — drop the override")
            url = dict(cfg["Url"][0])
            if "path" in pin:
                url["Path"] = pin["path"]
            if "status" in pin:
                url["Status"] = pin["status"]
            cfg["Url"] = [url]
        if "credlist" in pin:
            if not cfg.get("CredLists"):
                raise SystemExit(
                    f"[event.conf] pin {pin!r} sets credlist but '{svc_name}' checks don't "
                    "authenticate — drop the override")
            cfg["CredLists"] = [f"{pin['credlist']}.credlist"]
        unknown = set(pin) - {"name", "vars", "score_only", "plant_only", "check",
                              *PIN_CHECK_OVERRIDES}
        if unknown:
            print(f"[event.conf] WARNING: ignoring unknown key(s) {sorted(unknown)} in pin "
                  f"{pin!r} — did you mean one of {PIN_CHECK_OVERRIDES}?")
    return svc_name, check_key, cfg


def expected_service_names(box_services: dict, boxes: list) -> set:
    """Scoreboard ServiceNames the current pins must produce (<box>-<Display>).

    Quotient registers every emitted check (Box.Web etc. are slices); these names are
    its global uniqueness domain, so verify gates the live set against this."""
    names = set()
    for box in boxes:
        for pin in box_services.get(box["name"], []):
            _, check_key, cfg = _resolve_pin(pin)
            if check_key is None:
                continue
            names.add(f"{box['name']}-{cfg['Display']}")
    return names


def build_event_conf(ctx: dict, box_services: dict) -> dict:
    teams      = ctx["teams"]
    boxes      = ctx["boxes_per_team"]
    passwords  = ctx["team_passwords"]
    event_name = ctx["event_name"]

    conf = {
        "RequiredSettings": {
            "EventName": event_name,
            "EventType": "rvb",
            "BindAddress": "0.0.0.0",
        },
        "MiscSettings": {
            "StartPaused": True,
            "Delay": 60,
            "Jitter": 10,
            "Points": 5,
        },
        "admin": [{"name": "admin", "pw": ctx["quotient_admin_password"]}],
        "team":  [
            {"name": team_key, "pw": passwords[team_key]}
            for team_key in teams
        ],
        "box": [],
    }

    if ctx.get("inject_password"):
        conf["inject"] = [{"name": "inject", "pw": ctx["inject_password"]}]

    referenced_credlists = set()

    for box in boxes:
        box_entry = {
            "name": box["name"],
            "ip":   f"192.168._.{box['last_octet']}",
        }

        seen_displays = set()
        for pin in box_services.get(box["name"], []):
            svc_name, check_key, check_cfg = _resolve_pin(pin)
            if check_key is None:
                if not (isinstance(pin, dict) and pin.get("plant_only")):
                    print(f"[event.conf] WARNING: no Quotient check type for '{svc_name}' on box '{box['name']}' — skipping")
                continue
            display = check_cfg["Display"]
            if display in seen_displays:
                raise SystemExit(
                    f"[event.conf] two pins on '{box['name']}' both render scoreboard name "
                    f"'{box['name']}-{display}' — Quotient requires unique <box>-<Display> check "
                    f"names. Differentiate one via a display override, e.g. "
                    f'{{"name": "{svc_name}", "display": "<alt>"}} in box_services.json.')
            seen_displays.add(display)
            box_entry.setdefault(check_key, []).append(check_cfg)
            if check_cfg.get("CredLists"):
                referenced_credlists.update(check_cfg["CredLists"])

        conf["box"].append(box_entry)

    if referenced_credlists:
        # Every credlist a check names must be declared AND pushed (push_event_conf
        # writes the matching file). Historically that was always linux.credlist;
        # packet dual-credit adds e.g. domain.credlist twins.
        conf["CredlistSettings"] = {
            "Credlist": [{"CredlistName": name, "CredlistPath": name}
                         for name in sorted(referenced_credlists)]
        }

    return conf


def _normalize_host(host: str) -> str:
    """Quotient's IP comes off Terraform as a bare address; requests needs a scheme."""
    if host.startswith("http://") or host.startswith("https://"):
        return host.rstrip("/")
    return f"http://{host}"


def _wait_for_quotient(host: str) -> None:
    deadline = time.time() + 120
    while True:
        try:
            requests.get(f"{host}/api/login", timeout=3)
            return
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
            if time.time() > deadline:
                raise SystemExit(f"[quotient] timed out waiting for {host} to come up")
            time.sleep(3)


def _admin_session(host: str, ctx: dict) -> requests.Session:
    session = requests.Session()
    r = session.post(f"{host}/api/login", json={"username": "admin", "password": ctx["quotient_admin_password"]})
    r.raise_for_status()
    print(f"[quotient] logged in as admin → {r.status_code}")
    return session


def seed_teams(host: str, ctx: dict) -> None:
    """Assign team identifiers and set started flag (idempotent)."""
    host = _normalize_host(host)
    _wait_for_quotient(host)
    session = _admin_session(host, ctx)

    r = session.get(f"{host}/api/teams")
    r.raise_for_status()
    existing = {t["Name"]: t["ID"] for t in r.json()}

    updates = []
    for team_key, identifier in ctx["teams"].items():
        if team_key not in existing:
            raise SystemExit(f"[quotient] team {team_key} not found — check event.conf seeding")
        updates.append({"id": existing[team_key], "identifier": identifier, "active": True})

    r = session.post(f"{host}/api/admin/teams", json={"teams": updates})
    r.raise_for_status()
    print(f"[quotient] updated {len(updates)} team identifiers → {r.status_code}")

    r = session.post(f"{host}/api/competition/start", json={"started": True})
    r.raise_for_status()
    print(f"[quotient] competition started (DB) → {r.status_code}")


def unpause_engine(host: str, ctx: dict) -> None:
    """Unblock scoring round loop (StartPaused=true). Not idempotent; gate with state flag."""

    host = _normalize_host(host)
    _wait_for_quotient(host)
    session = _admin_session(host, ctx)
    r = session.post(f"{host}/api/engine/pause", json={"pause": False})
    r.raise_for_status()
    print(f"[quotient] engine unpaused → {r.status_code}")


def engine_paused(host: str, ctx: dict):
    """The engine's actual pause state, or None when the endpoint won't say.

    Lets a resume ask the engine instead of blindly re-POSTing after a crash
    in the window between the unpause POST and the state-flag save."""
    host = _normalize_host(host)
    try:
        session = _admin_session(host, ctx)
        r = session.get(f"{host}/api/engine/pause", timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "paused" in data:
            return bool(data["paused"])
        return None
    except Exception:
        return None


def create_injects(host: str, admin_password: str, injects: list) -> tuple:
    """Create injects via Quotient API (multipart POST). Skips titles already present.

    Returns (created_count, failed_titles); a resume re-runs safely (dedup on
    titles), so the deploy only records injects_created when nothing failed."""

    if not injects:
        return 0, []

    host = _normalize_host(host)
    session = requests.Session()
    r = session.post(f"{host}/api/login", json={"username": "admin", "password": admin_password})
    r.raise_for_status()

    existing_titles = set()
    try:
        r = session.get(f"{host}/api/injects", timeout=10)
        r.raise_for_status()
        existing_titles = {inj.get("Title") or inj.get("title") for inj in (r.json() or [])}
    except requests.RequestException as e:
        print(f"[quotient] WARNING: couldn't fetch existing injects ({e}) — proceeding without "
              "dedup, a resume may create duplicates")

    created = 0
    failed = []
    for inj in injects:
        if inj["title"] in existing_titles:
            print(f"[quotient] inject '{inj['title']}' already exists — skipping")
            continue
        parts = [
            ("title",       (None, inj["title"])),
            ("description", (None, inj["description"])),
            ("open-time",   (None, inj["open_time"])),
            ("due-time",    (None, inj["due_time"])),
            ("close-time",  (None, inj["close_time"])),
        ]
        open_handles = []
        for fpath in inj.get("files", []):
            p = Path(fpath)
            fh = p.open("rb")
            open_handles.append(fh)
            parts.append(("files", (p.name, fh)))
        try:
            r = session.post(f"{host}/api/injects/create", files=parts)
        finally:
            for fh in open_handles:
                fh.close()
        if r.status_code >= 400:
            print(f"[quotient] WARNING: inject '{inj['title']}' failed → {r.status_code} {r.text[:200]}")
            failed.append(inj["title"])
        else:
            print(f"[quotient] created inject '{inj['title']}' → {r.status_code}")
            created += 1
    return created, failed
