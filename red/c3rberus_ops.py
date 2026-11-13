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
import shlex
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


def extend_transport_map(ports):
    """The sliver transport map plus this platform's own listener entries (gateway ==
    server, the same-port shape tlaloc's entries use — the engine firewall timer rewrites
    everything from this map, so an absent entry is an unreachable C2)."""
    out = dict(ports or {})
    for name, port in zip(("c3rberus-mtls", "c3rberus-stage"), PLATFORM_LISTENER_PORTS):
        out.setdefault(name, {"gateway": port, "server": port})
    return out


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


def _login_or_none(ssh_key, red_ip, password, *, runner=None):
    try:
        return _login(ssh_key, red_ip, password, runner=runner)
    except RuntimeError:
        return None


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


def render_inventory(comp_dir, box_pw, *, red_ip, fetch_source=None):
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
    # The scoring identity resolves box.scoring_password → .env → box admin, so the
    # engine renders NO per-box scoring_password: the seeded value in .env is what
    # the creator actually provisions on every box (AD account for domain Windows,
    # local SAM for members, uid-0 via _provision_linux_scoring for Linux). Pinning
    # the packet decoy pw strands the domain channel — MIRA\red_scoring LOGON_FAILURE
    # rotation storm on members (run-fca282ee).
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
            if fetch_source:
                # HMAC fetch tokens bind to the source ip the script server sees; the
                # lab router NATs every box, so a box_ip-signed token 401s with
                # signature-mismatch on every fetch (run-4f784bed) — sign the NAT ip.
                entry["fetch_source_ip"] = fetch_source
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
        # combined deploy scripts ride curl+wget fetch-retry tails (~185s worst
        # case); at the stock 120-180s window nxc kills the ssh, SIGHUP skips the
        # result POST, and the awaited future orphans — deploy_all then NEVER
        # completes (run-64f7c02a: 28 children parked 'running' for 45 min).
        "NXC_DEPLOY_TIMEOUT=300",
        # The installed build's put_file path (pivot + per-box putfile acquires)
        # still uses TWO run_in_executor submissions — the single-task
        # _acquire_both fix covers _run only. Under a full deploy-all the
        # acquire pool (NXC_MAX_CONCURRENT+20 threads) fills with stage-1
        # holders and stage-2 acquires starve: zero nxc, zero timeouts, forever
        # (run-dfccb687 froze 17:37 with 40/40 threads parked; the fork's own
        # wrapper.py comment logs the identical 2026-06-20 incident). Pool =
        # limit+20, so a bigger limit leaves free threads for release paths;
        # real exec concurrency stays capped by PER_BOX=10 x 8 boxes either way.
        "NXC_MAX_CONCURRENT=140",
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
        # the platform's own vocabulary (task_tracker.py: pending -> running ->
        # done/failed/cancelled) — "completed" was a guess that stalled a finished
        # preflight for its whole 2400s budget (live run-54d28d56, 2026-11-13)
        if doc.get("status") in ("done", "failed", "cancelled", "completed", "error"):
            return doc.get("status")
        time.sleep(15)
    return "timeout"


def _provision_linux_scoring(ssh_key, red_ip, inv_doc, box_pw, scoring_pw, *, runner=None):
    """The platform's scoring contract (config.py RED_SCORING): a password-auth'd
    uid-0 account on Linux — its IOC deploys are plain nxc put-files, so root
    parity is structural. The packet's uid-1010 decoy authenticates but cannot
    write System32//usr/local (Errno 13; runs 666952e9/702607be/da75e935), and a
    second uid-0 account is PAM-refused under the goldens' prohibit-password
    sshd. So: pin red_scoring to uid 0 with the seeded scoring password (direct
    passwd edit — usermod refuses a uid-0 account sharing PID1) + a scoped
    PermitRootLogin drop-in. Convergent/idempotent."""
    script = (
        "python3 -c \"import re;L=open('/etc/passwd').read();"
        "L=re.sub(r'^red_scoring:[^:]*:[^:]*:[^:]*:[^:]*:[^:]*:[^:]*$',"
        "'red_scoring:x:0:0::/root:/bin/bash',L,flags=re.M);"
        "open('/etc/passwd','w').write(L)\"; "
        "usermod -s /bin/bash red_scoring 2>/dev/null; "
        "printf 'red_scoring:%s\\n' | chpasswd; "
        "echo 'PermitRootLogin yes' > /etc/ssh/sshd_config.d/99-tez-scoring.conf; "
        "systemctl reload ssh 2>/dev/null || systemctl reload sshd; sleep 2; "
        "[ \"$(id -u red_scoring)\" = 0 ] && echo TEZPROVOK"
        % scoring_pw)
    import re as _re
    for team in inv_doc["teams"]:
        for box in team["boxes"]:
            if box["os"] != "linux":
                continue
            r = _ssh(ssh_key, red_ip,
                     f"nxc ssh {box['ip']} -u {box['username']} "
                     f"-p {shlex.quote(box_pw)} -x "
                     f"{shlex.quote('sudo -n bash -c ' + shlex.quote(script))}",
                     runner=runner, timeout=120, check=False)
            out = (r.stdout or "") + (r.stderr or "")
            # success = the sentinel echoed only when uid==0 — nxc colorizes and
            # prefixes lines, so parsing its formatted tail fooled endswith("0") on a
            # PROVISIONED box (trailing ANSI reset, run-f206355b).
            clean = _re.sub(r"\x1b\[[0-9;]*m", "", out)
            if "TEZPROVOK" not in clean:
                raise RuntimeError(f"linux scoring provisioning failed on {box['ip']}: "
                                   f"{clean.strip()[-160:]}")


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
               body={"include_implants": True, "skip_active": False, "team_nums": team_nums},
               runner=runner, timeout=60)
    if not doc.get("task_id"):
        raise RuntimeError(f"deploy-all-iocs refused: {doc}")
    status = _wait_task(ssh_key, red_ip, token, doc["task_id"], runner=runner, timeout_s=5400)
    rows = _api(ssh_key, red_ip, token, "GET", "/c2/iocs/deployed", runner=runner)
    deployed = len(rows) if isinstance(rows, list) else 0
    print(f"  c3rberus    : deploy-all-iocs {status}; {deployed} deployed rows")
    if status not in ("done", "completed"):
        # never stamp a half-planted catalog as the day-0 baseline (run-3f39134d:
        # 21 windows deploys died on an unprovisioned scoring account)
        raise RuntimeError(f"deploy-all-iocs ended {status} — catalog not fully planted")
    return deployed


def _probe_fetch_source(ssh_key, red_ip, comp_dir, box_pw, *, runner=None):
    """What source ip does the script server actually see a box fetch come from?
    The lab router NATs every box LAN->lab, and HMAC fetch tokens bind to that
    observed ip — a box_ip-signed token 401s signature-mismatch on EVERY deploy
    fetch (run-4f784bed). Ask live rather than trusting any constant."""
    box_user = "blueteam"
    try:
        box_user = json.loads((Path(comp_dir) / "users.json").read_text()).get(
            "box_username", box_user)
    except (OSError, ValueError):
        pass
    for rec in sorted(_teams(comp_dir).values(), key=lambda r: r["identifier"]):
        for box in _boxes(comp_dir):
            kind = box_os(box.get("template"))
            if kind == "linux" and not box.get("in_path") and kind != "firewall":
                ip = f"192.168.{rec['identifier']}.{box['last_octet']}"
                break
        else:
            continue
        break
    else:
        raise RuntimeError("no linux box to probe for the fetch source")
    tag = "zz" + secrets.token_hex(6)
    # a Bearer-less GET 401s BEFORE the (logged) HMAC check — send a syntactically
    # valid junk token so the request travels the same signature-mismatch path a
    # real deploy fetch does, which is what records client= in the log.
    exp = int(time.time()) + 600
    r = _ssh(ssh_key, red_ip,
             f"nxc ssh {ip} -u {box_user} -p {shlex.quote(box_pw)} -x "
             f"'curl -sk --max-time 8 -H \"Authorization: Bearer {tag}.{exp}\" "
             f"https://{red_ip}:4000/scripts/wrapper/{tag}' "
             f">/dev/null 2>&1; cd /opt/c3rberus && "
             f"sudo -n grep -rah {tag} logs/ 2>/dev/null "
             f"| grep -oE 'client=[0-9.]+' | tail -1 | cut -d= -f2",
             runner=runner, timeout=120, check=False)
    nat = (r.stdout or "").strip().splitlines()[-1].strip() if (r.stdout or "").strip() else ""
    if not nat.count(".") == 3:
        raise RuntimeError(f"fetch-source probe saw nothing from {ip} — script server down? "
                           f"({nat!r})")
    print(f"  c3rberus    : boxes reach the script server as {nat} (lab NAT source)")
    return nat


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
    # Inventory was written blind (no server to probe during first install); now
    # that the script server answers, pin the real fetch source and re-boot the
    # backend so its boot-derived inventory/credentials carry the corrected boxes.
    nat = _probe_fetch_source(ssh_key, red_ip, comp_dir, box_pw, runner=runner)
    _ssh(ssh_key, red_ip, "sudo -n tee /opt/c3rberus/config/inventory.yml >/dev/null",
         input=render_inventory(comp_dir, box_pw, red_ip=red_ip, fetch_source=nat),
         runner=runner, timeout=120)
    _ssh(ssh_key, red_ip, "sudo -n systemctl restart c3rberus-backend",
         runner=runner, timeout=60)
    wait_backend(ssh_key, red_ip, runner=runner)
    # The seed must be on disk BEFORE anything can raise after it: a rotated admin
    # password that was never persisted strands the platform behind credentials nobody
    # holds (run-3a343726, parity died between rotate and write).
    if seed.get("admin_password"):
        token, user_id = _login(ssh_key, red_ip, seed["admin_password"], runner=runner)
    else:
        login = _login_or_none(ssh_key, red_ip, DEFAULT_ADMIN[1], runner=runner)
        if login is None:
            # A crashed earlier stage rotated and lost the password. Pre-plant the DB is
            # derived state — the backend's startup init rebuilds it from inventory.
            print("  c3rberus    : WARNING — reinitializing the platform database (a prior "
                  "stage rotated admin without persisting the seed)")
            _ssh(ssh_key, red_ip,
                 "sudo -n systemctl stop c3rberus-backend && sudo -n rm -f "
                 f"{INSTALL_DIR}/db/database.db* && sudo -n systemctl start c3rberus-backend",
                 runner=runner, timeout=180)
            wait_backend(ssh_key, red_ip, runner=runner)
            login = _login(ssh_key, red_ip, DEFAULT_ADMIN[1], runner=runner)
        token, user_id = login
        # the published db_init password is public knowledge — rotate on first boot
        _api(ssh_key, red_ip, token, "POST", "/admin/change_password",
             body={"user_id": user_id, "password": admin_pw}, runner=runner)
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_text(json.dumps({"admin_password": admin_pw,
                                      "scoring_password": scoring_pw, "red_ip": red_ip}))
        token, _ = _login(ssh_key, red_ip, admin_pw, runner=runner)
    board = _api(ssh_key, red_ip, token, "GET", "/boxes", runner=runner)
    seen = {ip for ip in _walk_ips(board)}
    missing = want_boxes(comp_dir) - seen
    if missing:
        raise RuntimeError(f"c3rberus's inventory shows {sorted(missing)} missing — the "
                           "rendered inventory does not match the range")
    team_nums = list(range(1, len(_teams(comp_dir)) + 1))
    # The platform's own pre-competition provisioning step: create the red_scoring
    # account that EVERY IOC deploy authenticates with (the script and the deploy
    # side resolve the password identically: seeded value in .env). The engine
    # shipped the credential without planting the identity — every Windows deploy
    # died LOGON_FAILURE (run-3f39134d, 2026-11-13).
    print("  c3rberus    : provisioning the scoring account on the boxes...")
    import yaml
    _provision_linux_scoring(ssh_key, red_ip, yaml.safe_load(
        render_inventory(comp_dir, box_pw, red_ip=red_ip)), box_pw, scoring_pw,
        runner=runner)
    _ssh(ssh_key, red_ip, f"cd {INSTALL_DIR} && sudo -n venv/bin/python "
         "scripts/infra/create_red_scoring.py", runner=runner, timeout=1800)
    cred_preflight(ssh_key, red_ip, token, team_nums, runner=runner)
    # always plant: deploy-all is idempotent (skip_active reuses, retries failures)
    # — a truthy count as a skip flag would freeze a half-planted catalog
    deployed = plant(ssh_key, red_ip, token, team_nums, runner=runner)
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
