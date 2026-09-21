"""Drives Quotient: build_event_conf(), seed_teams(), unpause_engine(), create_injects()."""

import time
from pathlib import Path

import requests

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
}


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

    needs_linux_credlist = False

    for box in boxes:
        box_entry = {
            "name": box["name"],
            "ip":   f"192.168._.{box['last_octet']}",
        }

        seen_checks = set()
        for svc_name in box_services.get(box["name"], []):
            if svc_name not in _SERVICE_TO_CHECK:
                print(f"[event.conf] WARNING: no Quotient check type for '{svc_name}' on box '{box['name']}' — skipping")
                continue
            check_key, check_cfg = _SERVICE_TO_CHECK[svc_name]
            dedup_key = (check_key, check_cfg.get("Port"))
            if dedup_key in seen_checks:
                continue
            seen_checks.add(dedup_key)
            box_entry.setdefault(check_key, []).append(check_cfg)
            if check_cfg.get("CredLists"):
                needs_linux_credlist = True

        conf["box"].append(box_entry)

    if needs_linux_credlist:
        conf["CredlistSettings"] = {
            "Credlist": [{"CredlistName": "linux.credlist", "CredlistPath": "linux.credlist"}]
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


def create_injects(host: str, admin_password: str, injects: list) -> None:
    """Create injects via Quotient API (multipart POST). Skips titles already present."""

    if not injects:
        return

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
        else:
            print(f"[quotient] created inject '{inj['title']}' → {r.status_code}")
