"""c3rberus as the range's red PLATFORM — installed on red01 alongside tlaloc.

tlaloc runs the campaign (C2, takedowns, restore detection); C3rberus runs the real
CDE IOC machinery on the same boxes — NetExec deploy/check/remediate + its own
scoreboard. One red team, two systems. The engine never edits the c3rberus repo: it
renders config/inventory.yml + .env from live estate state, installs via a bootstrap
script (red/assets/c3rberus-bootstrap.sh), and drives the documented bearer-JWT HTTP
API from red01 itself. Lifecycle + design notes: docs/internals.md § c3rberus.
"""

import json
import secrets
import subprocess
import time
from pathlib import Path

from red.tlaloc_ops import compfile_lines

REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / "red" / "assets" / "c3rberus-bootstrap.sh"

INSTALL_DIR = "/opt/c3rberus"
API = "https://localhost:3000"              # curl always runs ON red01 against itself
DEFAULT_ADMIN = ("admin", "phoenix-collide-surprise-oaf")   # db_init.py:628-641, published
SEED_FILE = "c3rberus-seed.json"
# The platform's own sliver listeners, offset from tlaloc's stock ports because both
# daemons live on red01. These values must match the unit + mint in
# red/assets/c3rberus-bootstrap.sh and the FORWARD extension in tlaloc_ops.
PLATFORM_LISTENER_PORTS = (38888, 38890)


def selected(comp_dir):
    return str(compfile_lines(Path(comp_dir)).get("c3rberus", "")).strip() \
        not in ("", "0", "off", "false")


def repo_url(comp_dir):
    import os
    return (compfile_lines(Path(comp_dir)).get("c3rberus_repo")
            or os.environ.get("TEZ_C3RBERUS_REPO")
            or "http://10.0.0.132:3000/hnasher1/c3rberus.git")


def _ssh(ssh_key, red_ip, remote, *, runner=None, timeout=900, input=None, check=True):
    from red import red01_ops
    runner = runner or subprocess.run
    argv = red01_ops.ssh_argv(ssh_key, red_ip, remote, timeout=25)
    kw = {"capture_output": True, "text": True, "timeout": timeout}
    if input is not None:
        kw["input"] = input
    try:
        proc = runner(argv, **kw)
    except subprocess.TimeoutExpired as e:
        if not check:
            class _T:
                returncode, stdout, stderr = 124, (e.stdout or b"").decode(errors="ignore"), \
                    (e.stderr or b"").decode(errors="ignore")
            return _T()
        raise
    if check and getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(f"c3rberus on red01 {red_ip}: {remote[:140]} — "
                           f"{((proc.stderr or '') + (proc.stdout or ''))[-300:]}")
    return proc


def _api(ssh_key, red_ip, token, method, path, *, body=None, runner=None, timeout=120):
    data = f"-H 'Content-Type: application/json' --data '{json.dumps(body)}' " if body else ""
    proc = _ssh(ssh_key, red_ip,
                f"curl -sk -m {timeout - 5} -X {method} -H 'Authorization: Bearer {token}' "
                f"{data}{API}{path}", runner=runner, timeout=timeout, check=False)
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError):
        return {"_raw": (proc.stdout or proc.stderr or "").strip()[-400:], "_rc": proc.returncode}


def _login(ssh_key, red_ip, password, *, runner=None):
    # /login takes a JSON LoginRequest and returns {"token", "user_id"} (routes/login.py:30)
    doc = _api(ssh_key, red_ip, "", "POST", "/login",
               body={"username": DEFAULT_ADMIN[0], "password": password}, runner=runner,
               timeout=30)
    if not isinstance(doc, dict) or not doc.get("token"):
        raise RuntimeError(f"c3rberus login refused: {doc}")
    return doc["token"], doc.get("user_id", 1)


# ---------------------------------------------------------------- inventory + env

def _compfile(comp_dir):
    return compfile_lines(Path(comp_dir))


def box_os(template):
    t = str(template or "").lower()
    if "win" in t:
        return "windows"
    if "pfsense" in t or "opnsense" in t or "vyos" in t:
        return "firewall"
    return "linux"


def _boxes(comp_dir):
    boxes = json.loads((Path(comp_dir) / "boxes.json").read_text())
    return list(boxes.values()) if isinstance(boxes, dict) else boxes


def _teams(comp_dir):
    return json.loads((Path(comp_dir) / "teams.json").read_text())


def render_inventory(comp_dir, box_pw, *, red_ip):
    """Live estate truth in c3rberus's inventory dialect.

    Explicit `os:` everywhere: c3rberus maps IOCs by exact string equality and its
    name heuristics would misclassify this estate (mail01→"windows"). In-path
    firewalls are excluded — infrastructure, not a target, and no red-scoring route
    to seed into them here."""
    import yaml
    roles = {}
    try:
        roles = json.loads((Path(comp_dir) / "domain_roles.json").read_text())
    except (OSError, ValueError):
        pass
    box_user = "blueteam"
    try:
        box_user = json.loads((Path(comp_dir) / "users.json").read_text()).get(
            "box_username", box_user)
    except (OSError, ValueError):
        pass
    prefix = str(_compfile(comp_dir).get("domain_prefix", "team"))
    suffix = str(_compfile(comp_dir).get("domain_suffix", ".local"))
    out = {"c2_settings": {"script_server_url": f"https://{red_ip}:4000",
                           "sliver_host": red_ip, "c2_ip": red_ip},
           "scoring_account": {"username": "red_scoring", "domain": "", "enabled": True},
           "teams": []}
    for n, (tname, rec) in enumerate(sorted(_teams(comp_dir).items()), 1):
        tid = rec["identifier"]
        domain = f"{prefix}{tid}{suffix}"
        netbios = domain.split(".")[0].upper()
        team_boxes = []
        for box in _boxes(comp_dir):
            kind = box_os(box.get("template"))
            if box.get("in_path") or kind == "firewall":
                continue
            entry = {"name": f"{box['name']}-{tid}", "ip": f"192.168.{tid}.{box['last_octet']}",
                     "os": kind, "username": box_user if kind == "linux" else ".\\Administrator",
                     "password": box_pw, "port": 22 if kind == "linux" else 5985}
            if kind == "windows":
                entry["connection"] = "winrm"
                entry["domain"] = domain
                entry["is_domain_joined"] = True
                entry["is_domain_controller"] = roles.get(box["name"]) == "dc"
                entry["domain_username"] = f"{netbios}\\Administrator"
                entry["domain_password"] = box_pw
            team_boxes.append(entry)
        out["teams"].append({"team_num": n, "name": tname, "boxes": team_boxes})
    return yaml.safe_dump(out, sort_keys=False)


def render_env(comp_dir, box_pw, *, red_ip, scoring_pw):
    lines = [
        "ENVIRONMENT=production", "DEV_MODE=False", "DB_DIR=./db",
        "USE_SCHEDULER=true", "CHECK_INTERVAL_MINUTES=5",
        "INVENTORY_FILE=config/inventory.yml",
        f"JWT_SECRET_KEY={secrets.token_urlsafe(48)}",
        f"SCRIPT_HMAC_SECRET={secrets.token_urlsafe(48)}",
        f"SCRIPT_ENCRYPTION_SALT={secrets.token_hex(16)}",
        "CSRF_PROTECT_MUTATIONS=false",
        f"C2_IP={red_ip}", f"SLIVER_HOST={red_ip}", "SLIVER_ENABLED=true",
        # tlaloc's daemon already holds the stock mTLS/stage ports on red01 — ours
        # bind offsets, or its listener create fails and the platform scores ghosts.
        "MTLS_LISTENER_PORT=%d" % PLATFORM_LISTENER_PORTS[0],
        "STAGE_LISTENER_PORT=%d" % PLATFORM_LISTENER_PORTS[1],
        "PIVOT_LISTENER_PORT_BASE=9900",
        f"SCRIPT_SERVER_URL=https://{red_ip}:4000",
        "SLIVER_CFG_PATH=./sliver_operator.cfg",
        "RED_SCORING_ENABLED=true", "RED_SCORING_USERNAME=red_scoring",
        "RED_SCORING_DOMAIN=", f"RED_SCORING_PASSWORD={scoring_pw}",
        "DEFAULT_WINDOWS_USERNAME=.\\Administrator", f"DEFAULT_WINDOWS_PASSWORD={box_pw}",
        "DEFAULT_LINUX_USERNAME=blueteam", f"DEFAULT_LINUX_PASSWORD={box_pw}",
        "NXC_MAX_CONCURRENT=20", "NXC_MAX_CONCURRENT_PER_BOX=3", "NXC_TIMEOUT=180",
        "CRED_REVALIDATION_INTERVAL_MINUTES=30",
        # keep the event pristine until the harness flips the first two at T0: boot
        # loops otherwise touch every box the moment the backend comes up.
        "SA_COLLECTION_ENABLED=false", "NMAP_HEALTH_ENABLED=false",
        "CRED_REVALIDATION_ENABLED=false", "PIVOT_KEEPALIVE_ENABLED=false",
        "DISPLAY_TIMEZONE=America/New_York", "LOG_LEVEL=INFO",
        "SLACK_ENABLED=false", "TEAMS_WEBHOOK_ENABLED=false",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- install + plant

def install(ssh_key, red_ip, *, comp_dir, box_pw, scoring_pw, runner=None):
    boot = BOOTSTRAP.read_text().replace("__REPO_URL__", repo_url(comp_dir))
    _ssh(ssh_key, red_ip, "sudo -n mkdir -p /opt/c3rberus-staging", runner=runner, timeout=60)
    for content, dest, mode in (
            (boot, "/opt/c3rberus-staging/bootstrap.sh", "0755"),
            (render_inventory(comp_dir, box_pw, red_ip=red_ip),
             "/opt/c3rberus-staging/inventory.yml", "0600"),
            (render_env(comp_dir, box_pw, red_ip=red_ip, scoring_pw=scoring_pw),
             "/opt/c3rberus-staging/env", "0600")):
        _ssh(ssh_key, red_ip,
             f"sudo -n tee {dest} >/dev/null && sudo -n chmod {mode} {dest}",
             runner=runner, timeout=120, input=content)
    print("  c3rberus    : bootstrapping on red01 (apt/pip/sliver from the cache/upstream, retried)...")
    r = _ssh(ssh_key, red_ip,
             "sudo -n bash /opt/c3rberus-staging/bootstrap.sh",
             runner=runner, timeout=7200, check=False)
    if getattr(r, "returncode", 1) != 0:
        raise RuntimeError("c3rberus bootstrap failed on red01:\n"
                           f"{((r.stdout or '') + (r.stderr or ''))[-1200:]}")
    print("  c3rberus    : bootstrap complete — sliver + backend units enabled")


def wait_backend(ssh_key, red_ip, *, runner=None, timeout_s=1500):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if "200" in (_ssh(ssh_key, red_ip,
                          f"curl -sk -m 8 -o /dev/null -w '%{{http_code}}' {API}/docs",
                          runner=runner, timeout=30, check=False).stdout or ""):
            return
        time.sleep(15)
    raise RuntimeError(f"the c3rberus backend never answered on red01 within {timeout_s}s "
                       "(check systemctl status c3rberus-backend + logs/uvicorn_*.log)")


def _wait_task(ssh_key, red_ip, token, task_id, *, runner=None, timeout_s=2400):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        doc = _api(ssh_key, red_ip, token, "GET", f"/c2/tasks/{task_id}", runner=runner,
                   timeout=30)
        if doc.get("status") in ("completed", "success", "failed", "cancelled", "error"):
            return doc.get("status")
        time.sleep(15)
    return "timeout"


def cred_preflight(ssh_key, red_ip, token, team_nums, *, runner=None):
    """Platform-side gate on its own scoring identity: pass | auth-fail |
    insufficient-perms | unreachable | absent, per box — an auth-fail list is the
    inventory drifting from the estate, caught before boxes hit production."""
    doc = _api(ssh_key, red_ip, token, "POST", "/admin/scoring-cred-preflight",
               body={"team_nums": team_nums}, runner=runner, timeout=60)
    status = _wait_task(ssh_key, red_ip, token, doc.get("task_id"), runner=runner)
    meta = (doc.get("metadata_json") or {}) if isinstance(doc, dict) else {}
    task = _api(ssh_key, red_ip, token, "GET", f"/c2/tasks/{doc.get('task_id')}",
                runner=runner, timeout=30)
    bad = {k: v for k, v in (task.get("metadata_json") or {}).items()
           if isinstance(v, dict) and v.get("reason") not in (None, "pass")}
    if bad:
        print(f"  c3rberus    : WARNING cred-preflight {status}, non-pass boxes: "
              f"{json.dumps(bad)[:400]}")
    return status, bad


def plant(ssh_key, red_ip, token, team_nums, *, runner=None):
    """Build the catalog and deploy it — c3rberus's assume-breach. Pre-competition
    state accepts deploy-all without the RUNNING confirm flag."""
    doc = _api(ssh_key, red_ip, token, "POST", "/admin/build-all-iocs",
               body={"include_implants": True, "force": False, "team_nums": team_nums},
               runner=runner, timeout=60)
    print(f"  c3rberus    : build-all-iocs {_wait_task(ssh_key, red_ip, token, doc.get('task_id'), runner=runner, timeout_s=3600)}")
    doc = _api(ssh_key, red_ip, token, "POST", "/admin/deploy-all-iocs",
               body={"include_implants": True, "skip_active": True, "team_nums": team_nums},
               runner=runner, timeout=60)
    if not doc.get("task_id"):
        raise RuntimeError(f"deploy-all-iocs refused: {doc}")
    status = _wait_task(ssh_key, red_ip, token, doc["task_id"], runner=runner, timeout_s=5400)
    rows = _api(ssh_key, red_ip, token, "GET", "/c2/iocs/deployed", runner=runner)
    deployed = len(rows) if isinstance(rows, list) else 0
    print(f"  c3rberus    : deploy-all-iocs {status}; {deployed} deployed rows")
    return deployed


def stage(comp_dir, *, ssh_key, red_ip, box_pw, runner=None, state=None):
    """Phase 7 / scrim hook, run AFTER tlaloc plants: install (adopt on re-stage),
    rotate the published admin password, prove the platform sees the range, plant
    its day-0 catalog once. The seed document keeps the credentials — the repo
    tree never sees them."""
    comp_dir = Path(comp_dir)
    seeded = comp_dir / "red" / SEED_FILE
    seed = json.loads(seeded.read_text()) if seeded.exists() else {}
    scoring_pw = seed.get("scoring_password") or secrets.token_urlsafe(15)
    admin_pw = seed.get("admin_password") or secrets.token_urlsafe(15)
    install(ssh_key, red_ip, comp_dir=comp_dir, box_pw=box_pw, scoring_pw=scoring_pw,
            runner=runner)
    wait_backend(ssh_key, red_ip, runner=runner)
    token, user_id = _login(ssh_key, red_ip, DEFAULT_ADMIN[1], runner=runner)
    if not seed.get("admin_password"):
        # the published db_init password is public knowledge — rotate on first boot
        doc = _api(ssh_key, red_ip, token, "POST", "/admin/change_password",
                   body={"user_id": user_id, "password": admin_pw}, runner=runner)
        token, _ = _login(ssh_key, red_ip, admin_pw, runner=runner)
    board = _api(ssh_key, red_ip, token, "GET", "/scoreboard", runner=runner)
    seen = {ip for ip in _walk_ips(board)}
    missing = want_boxes(comp_dir) - seen
    if missing:
        raise RuntimeError(f"c3rberus's scoreboard never loaded {sorted(missing)} — the "
                           "rendered inventory does not match the range")
    team_nums = list(range(1, len(_teams(comp_dir)) + 1))
    cred_preflight(ssh_key, red_ip, token, team_nums, runner=runner)
    deployed = seed.get("deployed") or plant(ssh_key, red_ip, token, team_nums,
                                            runner=runner)
    seeded.parent.mkdir(parents=True, exist_ok=True)
    seeded.write_text(json.dumps({"deployed": deployed, "admin_password": admin_pw,
                                  "scoring_password": scoring_pw, "red_ip": red_ip,
                                  "team_nums": team_nums, "boxes": sorted(seen)}, indent=2))
    print(f"  c3rberus    : day-0 catalog planted — {deployed} IOC deploys across "
          f"{len(seen)} boxes (scoring starts at T0)")
    return {"platform": "c3rberus", "red_ip": red_ip, "deployed": deployed,
            "boxes": sorted(seen)}


def _walk_ips(doc):
    if isinstance(doc, dict):
        for k, v in doc.items():
            if k == "ip" and isinstance(v, str):
                yield v
            else:
                yield from _walk_ips(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from _walk_ips(v)


def want_boxes(comp_dir):
    want = set()
    for tname, rec in sorted(_teams(comp_dir).items()):
        for box in _boxes(comp_dir):
            if box.get("in_path") or box_os(box.get("template")) == "firewall":
                continue
            want.add(f"192.168.{rec['identifier']}.{box['last_octet']}")
    return want


def _token_for(comp_dir, ssh_key, red_ip, runner=None):
    seed = json.loads((Path(comp_dir) / "red" / SEED_FILE).read_text())
    token, _ = _login(ssh_key, seed.get("red_ip") or red_ip, seed["admin_password"],
                      runner=runner)
    return token, seed


def begin(comp_dir, *, ssh_key, red_ip=None, runner=None):
    """T0 hook: flip the box-touching loops on and start the competition — the
    scheduler fires its first check cycle immediately (start_comp semantics)."""
    seeded = Path(comp_dir) / "red" / SEED_FILE
    if not seeded.exists():
        return False
    token, seed = _token_for(comp_dir, ssh_key, red_ip, runner=runner)
    _api(ssh_key, seed["red_ip"], token, "PATCH", "/admin/settings",
         body={"SA_COLLECTION_ENABLED": True, "NMAP_HEALTH_ENABLED": True}, runner=runner)
    doc = _api(ssh_key, seed["red_ip"], token, "POST", "/admin/start_comp", runner=runner)
    print(f"  c3rberus    : comp started {str(doc)[:120]}")
    return True


def end(comp_dir, *, ssh_key, red_ip=None, runner=None):
    """Stop scoring cleanly (clears playbook jobs + scheduler) before any harvest."""
    seeded = Path(comp_dir) / "red" / SEED_FILE
    if not seeded.exists():
        return False
    token, seed = _token_for(comp_dir, ssh_key, red_ip, runner=runner)
    _api(ssh_key, seed["red_ip"], token, "POST", "/admin/stop_comp", runner=runner)
    return True


def harvest(comp_dir, evidence_dir, *, ssh_key, red_ip=None, runner=None):
    """The AAR truth: a consistent sqlite snapshot (.backup — the WAL must be
    folded, never a raw cp mid-write), logs, and the rendered inventory."""
    seeded = Path(comp_dir) / "red" / SEED_FILE
    if not seeded.exists():
        return None
    seed = json.loads(seeded.read_text())
    red_ip = red_ip or seed.get("red_ip")
    dest = Path(evidence_dir) / "c3rberus"
    dest.mkdir(parents=True, exist_ok=True)
    _ssh(ssh_key, red_ip,
         f"cd {INSTALL_DIR} && sudo -n sqlite3 db/database.db '.backup /tmp/c3r-aar.db' "
         "2>/dev/null || true; sudo -n cp /tmp/c3r-aar.db /tmp/c3r-aar.db.done 2>/dev/null || true",
         runner=runner, timeout=120, check=False)
    got = []
    for remote, local in ((f"/tmp/c3r-aar.db", "database.db"),
                          (f"{INSTALL_DIR}/logs", "logs"),
                          (f"{INSTALL_DIR}/config/inventory.yml", "inventory.yml")):
        r = _ssh(ssh_key, red_ip,
                 f"sudo -n tar -czf - -C $(dirname {remote}) $(basename {remote}) 2>/dev/null",
                 runner=runner, timeout=300, check=False)
        blob = (r.stdout or "").encode(errors="ignore")
        if blob[:2] == b"\x1f\x8b":
            import tarfile, io
            with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
                tar.extractall(dest, filter="data")
            got.append(local)
    Path(dest, "seed.json").write_text(json.dumps(
        {k: seed[k] for k in ("deployed", "red_ip", "boxes", "team_nums") if k in seed},
        indent=2))
    return {"dir": str(dest), "got": got}


def verdict(comp_dir, evidence_dir):
    """c3rberus's reality beside tlaloc's verdict: what was planted, what blue beat
    back — straight from the scoring view in the harvested DB."""
    import sqlite3
    out = {"platform": "c3rberus", "planted": None, "teams": {}, "note": ""}
    seeded = Path(comp_dir) / "red" / SEED_FILE
    if seeded.exists():
        out["planted"] = json.loads(seeded.read_text()).get("deployed")
    db = Path(evidence_dir) / "c3rberus" / "database.db"
    if not db.exists():
        out["note"] = "no c3rberus DB in the harvest"
        return out
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
        if "deployed_iocs" in tables and "boxes" in tables:
            for team, deployed, active, remediated in con.execute(
                    "SELECT b.team_num, COUNT(*), "
                    "SUM(CASE WHEN d.last_effect_status=1 THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN d.last_effect_status=0 THEN 1 ELSE 0 END) "
                    "FROM deployed_iocs d JOIN boxes b ON b.box_ip=d.box_ip "
                    "GROUP BY b.team_num"):
                out["teams"][str(team)] = {"deployed": deployed, "active": active,
                                           "remediated": remediated}
        if "score_history" in tables:
            for team, points in con.execute(
                    "SELECT team_num, SUM(points) FROM score_history GROUP BY 1"):
                out["teams"].setdefault(str(team), {})["points"] = points
    except sqlite3.Error as e:
        out["note"] = f"db read failed: {e}"
    finally:
        con.close()
    Path(evidence_dir, "c3rberus-verdict.json").write_text(json.dumps(out, indent=2))
    return out
