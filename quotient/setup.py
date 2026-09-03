"""
Drives Quotient (the scoring engine): build_event_conf() generates its TOML scoring
config from Terraform's agent_context; seed_teams() logs into its admin API to seed team
identifiers and start the competition clock once that config is live; unpause_engine()
separately unblocks the scoring round loop (split out from seed_teams() because that one
call isn't safely repeatable — see its docstring).
"""

import time
from pathlib import Path

import requests

from utils import BOX_PASSWORD, BOX_USERNAME_DEFAULT

# Maps a nakon service name to its Quotient check key + config dict.
# Keys and field names must match Quotient's Go struct TOML tags exactly (case-sensitive
# for the check-type key on Box; field names are matched case-insensitively by BurntSushi).
# A box can accumulate multiple checks (e.g. apache + bind → Web + Dns).
# Vulns and unrecognised service names are skipped with a warning.
_SERVICE_TO_CHECK = {
    # Web (HTTP) — Box.Web field; Url is a required nested array of {Path, Status}
    "apache":    ("Web",  {"Display": "http",      "Port": 80,   "Scheme": "http", "Url": [{"Path": "/", "Status": 200}]}),
    "nginx":     ("Web",  {"Display": "http",      "Port": 80,   "Scheme": "http", "Url": [{"Path": "/", "Status": 200}]}),
    "httpd":     ("Web",  {"Display": "http",      "Port": 80,   "Scheme": "http", "Url": [{"Path": "/", "Status": 200}]}),
    "splunk":    ("Web",  {"Display": "splunk",    "Port": 8000, "Scheme": "http", "Url": [{"Path": "/", "Status": 200}]}),
    "roundcube": ("Web",  {"Display": "roundcube", "Port": 80,   "Scheme": "http", "Url": [{"Path": "/", "Status": 200}]}),
    # DNS — Box.Dns field; Record is a required array of {Kind, Domain, Answer}
    "bind":      ("Dns",  {"Display": "dns",  "Port": 53, "Record": [{"Kind": "A", "Domain": "localhost", "Answer": ["127.0.0.1"]}]}),
    "named":     ("Dns",  {"Display": "dns",  "Port": 53, "Record": [{"Kind": "A", "Domain": "localhost", "Answer": ["127.0.0.1"]}]}),
    # SSH — Box.Ssh field; CredLists required for login check
    "ssh":       ("Ssh",  {"Display": "ssh",  "Port": 22,  "CredLists": ["linux.credlist"]}),
    "openssh":   ("Ssh",  {"Display": "ssh",  "Port": 22,  "CredLists": ["linux.credlist"]}),
    "sshd":      ("Ssh",  {"Display": "ssh",  "Port": 22,  "CredLists": ["linux.credlist"]}),
    # FTP — Box.Ftp field. Use an authenticated login against linux.credlist (the same
    # admin/user1/user2 accounts SMTP scores against): the `unauthorized-ftp-server` config
    # installs vsftpd with Ubuntu's default anonymous_enable=NO / local_enable=YES, so an
    # anonymous check can never pass but a local login does. (Keep box config + check in sync.)
    "vsftpd":    ("Ftp",  {"Display": "ftp",  "Port": 21,  "CredLists": ["linux.credlist"]}),
    "ftpd":      ("Ftp",  {"Display": "ftp",  "Port": 21,  "CredLists": ["linux.credlist"]}),
    "ftp":       ("Ftp",  {"Display": "ftp",  "Port": 21,  "CredLists": ["linux.credlist"]}),
    # SMTP — Box.Smtp field; smtp.go always calls getCreds so CredLists is required
    "postfix":   ("Smtp", {"Display": "smtp", "Port": 25,  "CredLists": ["linux.credlist"]}),
    "sendmail":  ("Smtp", {"Display": "smtp", "Port": 25,  "CredLists": ["linux.credlist"]}),
    "exim":      ("Smtp", {"Display": "smtp", "Port": 25,  "CredLists": ["linux.credlist"]}),
    "exim4":     ("Smtp", {"Display": "smtp", "Port": 25,  "CredLists": ["linux.credlist"]}),
    # IMAP — Box.Imap field; CredLists triggers authenticated mailbox-list check
    "dovecot":   ("Imap", {"Display": "imap", "Port": 143, "CredLists": ["linux.credlist"]}),
    "cyrus":     ("Imap", {"Display": "imap", "Port": 143, "CredLists": ["linux.credlist"]}),
    # SQL — Box.Sql field; Kind defaults to "mysql" but must be explicit; needs CredLists to login.
    # Unlike the checks above, these authenticate against the database's own user table rather
    # than a system account. create-competition.py's fix_services_on_boxes binds mariadb/mysql to
    # 0.0.0.0 and creates the admin/user1/user2 DB users with the linux.credlist passwords
    # (CREATE USER ... @'%' + GRANT ALL), so the same credlist that satisfies SSH/SMTP logs in
    # here too and a healthy box scores UP. (Keep box config + check in sync — the box grants the
    # DB users, so don't drop CredLists.)
    "mariadb":   ("Sql",  {"Display": "sql",  "Port": 3306, "Kind": "mysql", "CredLists": ["linux.credlist"]}),
    "mysql":     ("Sql",  {"Display": "sql",  "Port": 3306, "Kind": "mysql", "CredLists": ["linux.credlist"]}),
    "mysqld":    ("Sql",  {"Display": "sql",  "Port": 3306, "Kind": "mysql", "CredLists": ["linux.credlist"]}),
    # Telnet — no protocol-aware Telnet check exists in Quotient, but it does ship a generic
    # Box.Tcp check (engine/checks/tcp.go: dials the port, scores UP on connect) that's exactly
    # enough to confirm the service is listening. Confirmed against /opt/quotient's own source
    # and config/event.conf.example on the scoring engine (2026-08-07).
    "telnet-service": ("Tcp", {"Display": "telnet", "Port": 23}),

    # Windows — Quotient has no SMB/RDP/WinRM-aware check type (only Web/Dns/Ssh/Ftp/Smtp/Imap/
    # Sql/Tcp exist anywhere in its engine), so these use the same generic Tcp port-open check
    # as telnet-service above. Keyed by the exact nakon catalog config name (these are
    # service-category Windows configs, not generic service binary names like "nginx" above —
    # box_services.json's entries for a Windows box are catalog config names verbatim).
    "Enable WinRM":   ("Tcp", {"Display": "winrm", "Port": 5985}),
    "New SMB Share":  ("Tcp", {"Display": "smb",   "Port": 445}),
    "RDP misconfigs": ("Tcp", {"Display": "rdp",   "Port": 3389}),
}


def build_event_conf(ctx: dict, box_services: dict) -> dict:
    teams      = ctx["teams"]          # {"team1": "1", "team2": "2"}
    boxes      = ctx["boxes_per_team"] # [{name, last_octet, cpu, ...}]
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

    # Inject-manager account. Quotient's INJECTAUTH-guarded routes (POST /api/injects/create,
    # announcements, submission downloads) accept the `admin` and `inject` roles; adding a
    # dedicated inject manager lets an organizer run injects without the full admin login.
    # Emitted whenever an inject password is supplied (i.e. the competition has an injects/ dir).
    if ctx.get("inject_password"):
        conf["inject"] = [{"name": "inject", "pw": ctx["inject_password"]}]

    needs_linux_credlist = False

    for box in boxes:
        box_entry = {
            "name": box["name"],
            "ip":   f"192.168._.{box['last_octet']}",  # _ = team identifier placeholder
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

    # box-level `credlists` entries are just names — Quotient resolves them against this
    # top-level registry (config/credlists/<CredlistPath> on the scoring engine)
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


def build_credlist() -> str:
    """Credlist CSV for Quotient login checks. Retained for reference;
    live deploys write linux.credlist inline via push_event_conf.
    """
    return f"{BOX_USERNAME_DEFAULT},{BOX_PASSWORD}\n"


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
    # Quotient's auth is cookie-based (POST /api/login sets a session cookie) — there's no
    # bearer token anywhere in the API.
    r = session.post(f"{host}/api/login", json={"username": "admin", "password": ctx["quotient_admin_password"]})
    r.raise_for_status()
    print(f"[quotient] logged in as admin → {r.status_code}")
    return session


def seed_teams(host: str, ctx: dict) -> None:
    """Assign team identifiers and set started flag (idempotent)."""
    # Look up IDs by name, then batch-update identifiers.
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
