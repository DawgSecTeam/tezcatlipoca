"""
Drives Quotient (the scoring engine): build_event_conf() generates its TOML scoring
config from Terraform's agent_context; seed_and_start() logs into its admin API to seed
team identifiers and start the competition clock once that config is live.
"""

import time

import requests

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
    # FTP — Box.Ftp field; no CredLists = anonymous login check
    "vsftpd":    ("Ftp",  {"Display": "ftp",  "Port": 21}),
    "ftpd":      ("Ftp",  {"Display": "ftp",  "Port": 21}),
    "ftp":       ("Ftp",  {"Display": "ftp",  "Port": 21}),
    # SMTP — Box.Smtp field; smtp.go always calls getCreds so CredLists is required
    "postfix":   ("Smtp", {"Display": "smtp", "Port": 25,  "CredLists": ["linux.credlist"]}),
    "sendmail":  ("Smtp", {"Display": "smtp", "Port": 25,  "CredLists": ["linux.credlist"]}),
    "exim":      ("Smtp", {"Display": "smtp", "Port": 25,  "CredLists": ["linux.credlist"]}),
    "exim4":     ("Smtp", {"Display": "smtp", "Port": 25,  "CredLists": ["linux.credlist"]}),
    # IMAP — Box.Imap field; CredLists triggers authenticated mailbox-list check
    "dovecot":   ("Imap", {"Display": "imap", "Port": 143, "CredLists": ["linux.credlist"]}),
    "cyrus":     ("Imap", {"Display": "imap", "Port": 143, "CredLists": ["linux.credlist"]}),
    # SQL — Box.Sql field; Kind defaults to "mysql" but must be explicit; needs CredLists to login
    "mariadb":   ("Sql",  {"Display": "sql",  "Port": 3306, "Kind": "mysql", "CredLists": ["linux.credlist"]}),
    "mysql":     ("Sql",  {"Display": "sql",  "Port": 3306, "Kind": "mysql", "CredLists": ["linux.credlist"]}),
    "mysqld":    ("Sql",  {"Display": "sql",  "Port": 3306, "Kind": "mysql", "CredLists": ["linux.credlist"]}),
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


def seed_and_start(host: str, ctx: dict) -> None:
    # Teams already exist by this point — event.conf's [[team]] entries seed name/pw at
    # startup — but Quotient assigns them their own numeric IDs, and `identifier` (used for
    # 192.168._.N substitution in box IPs) isn't settable from the TOML at all. Look the IDs
    # up by name, then batch-update identifiers in a single call (that's the only shape
    # /api/admin/teams accepts).
    # Wait for Quotient to accept connections — docker compose restart can take >10 s
    deadline = time.time() + 120
    while True:
        try:
            requests.get(f"{host}/api/login", timeout=3)
            break
        except requests.exceptions.ConnectionError:
            if time.time() > deadline:
                raise SystemExit(f"[quotient] timed out waiting for {host} to come up")
            time.sleep(3)

    session = requests.Session()

    # Quotient's auth is cookie-based (POST /api/login sets a session cookie) — there's no
    # bearer token anywhere in the API.
    r = session.post(f"{host}/api/login", json={"username": "admin", "password": ctx["quotient_admin_password"]})
    r.raise_for_status()
    print(f"[quotient] logged in as admin → {r.status_code}")

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

    # /api/competition/start only updates the DB flag — the engine goroutine stays blocked
    # on EnginePauseWg.Wait() (because event.conf has StartPaused=true) until this call
    # decrements the WaitGroup and allows the round loop to proceed.
    r = session.post(f"{host}/api/engine/pause", json={"pause": False})
    r.raise_for_status()
    print(f"[quotient] engine unpaused → {r.status_code}")
