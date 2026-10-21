import json
import os
import re
import secrets as _secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common.comp_paths import comps_root
from estate import config


def local_python():
    """Interpreter for operator-side children: a distro python3 may lack toml/paramiko, so use the harness's own (TEZ_PYTHON overrides)."""
    return config.active().knobs.python or sys.executable

_USERNAME_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_COMP_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")

PRINT_LOCK = threading.Lock()
MAX_CONCURRENCY = 8

# Failed prerequisites the deploy continued past; collect so a dead range has a named cause.
_DEGRADATIONS = []
_DEGRADATION_LOCK = threading.Lock()


def record_degradation(what, detail=""):
    import time

    entry = {"what": str(what), "detail": str(detail)[:300],
             "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with _DEGRADATION_LOCK:
        _DEGRADATIONS.append(entry)
    return entry


def degradations():
    with _DEGRADATION_LOCK:
        return [dict(entry) for entry in _DEGRADATIONS]


def clear_degradations():
    with _DEGRADATION_LOCK:
        _DEGRADATIONS.clear()


def degradation_summary():
    seen = {}
    for entry in degradations():
        key = (entry["what"], entry["detail"])
        seen[key] = seen.get(key, 0) + 1
    return [{"what": what, "detail": detail, "count": count}
            for (what, detail), count in seen.items()]


def mint_run_id():
# Teardown requires this per-deploy id; concurrent deploys of one comp dir cannot destroy each other's VMs.
    return f"run-{_secrets.token_hex(4)}"


def retry(fn, *, attempts, gap, backoff=False, retry_on=Exception):
    predicate = callable(retry_on) and not (isinstance(retry_on, type)
                                            and issubclass(retry_on, BaseException))
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if not (retry_on(exc) if predicate else isinstance(exc, retry_on)):
                raise
            if attempt == attempts:
                raise
            time.sleep(gap * attempt if backoff else gap)


def run_concurrent(items, fn, max_workers=MAX_CONCURRENCY):
# Exceptions are returned in the list, never raised; wrap prints in PRINT_LOCK.
    results = [None] * len(items)
    if not items:
        return results
    workers = max(1, min(max_workers, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, item): idx for idx, item in enumerate(items)}
        for future, idx in futures.items():
            try:
                results[idx] = future.result()
            except Exception as exc:
                results[idx] = exc
    return results


def is_unmanaged(box):
# Skipped by every plant/config/scoring path; flag lives in boxes.json so per-box-index vmids stay positional.
    return bool(box.get("unmanaged"))


def is_in_path_fw(box):
# WAN on vmbrW<id>, LAN on vmbr<id>; deploy phase 5 bootstraps its pfSense so the engine routes the team subnet.
    return is_unmanaged(box) and bool(box.get("in_path"))


def valid_unix_username(name):
    return bool(name) and _USERNAME_RE.fullmatch(name) is not None


# cloud-init adopts a colliding legacy system account instead of creating it, landing the
# sudoers rule and SSH key on a nologin shadow so every ssh <name>@box fails.
LEGACY_ACCOUNT_NAMES = frozenset({
    "root", "bin", "daemon", "adm", "lp", "sync", "shutdown", "halt", "mail",
    "news", "uucp", "operator", "games", "man", "ftp", "proxy", "www-data",
    "backup", "list", "irc", "gnats", "nobody", "systemd-network",
    "systemd-resolve", "systemd-timesync", "messagebus", "sshd", "dbus",
    "avahi", "avahi-autoipd", "tty", "disk", "kmem", "mem", "wheel", "shadow",
    "utmp", "video", "audio", "floppy", "tape", "uuidd", "tcpdump", "tss",
    "polkitd", "rtkit", "pulse", "qemu", "gdm",
})


def is_legacy_account_name(name):
    return name in LEGACY_ACCOUNT_NAMES


def valid_comp_name(name):
    return bool(name) and ".." not in name and _COMP_NAME_RE.fullmatch(name) is not None

# A hardcoded public resolver broke every box on an uplink that filters it (live: run-fee03a45
# could not resolve download.docker.com); TEZ_BOX_DNS lets a placement name its own resolver
# first (the range gateway), with public ones as fallback.
BOX_DNS = (os.environ.get("TEZ_BOX_DNS") or "1.1.1.1 8.8.8.8").strip()
BOX_DNS_PRIMARY = BOX_DNS.split()[0]
_DNS_RESOLV = "".join(f"nameserver {ns}\\n" for ns in BOX_DNS.split())
DNS_FIX_CMD = (
    f'printf "{_DNS_RESOLV}" | sudo tee /etc/resolv.conf; '
    f'printf "{_DNS_RESOLV}" | sudo tee /etc/resolv.conf.head; '
    "sudo mkdir -p /etc/systemd/resolved.conf.d; "
    f'printf "[Resolve]\\nDNS={BOX_DNS}\\n" | sudo tee /etc/systemd/resolved.conf.d/upstream.conf; '
    "sudo systemctl restart systemd-resolved 2>/dev/null || true"
)

DNS_FIX_CMD_ROOT = DNS_FIX_CMD.replace("sudo ", "")


BOX_USERNAME_DEFAULT = "ubuntu"
CREDLIST_USERNAMES_DEFAULT = ["admin", "user1", "user2"]


def load_users_config(comp_dir):
    path = Path(comp_dir) / "users.json"
    if not path.exists():
        return BOX_USERNAME_DEFAULT, list(CREDLIST_USERNAMES_DEFAULT)

    data = json.loads(path.read_text())
    box_username = data.get("box_username") or BOX_USERNAME_DEFAULT
    credlist_usernames = data.get("credlist_usernames") or list(CREDLIST_USERNAMES_DEFAULT)
    if not valid_unix_username(box_username):
        box_username = BOX_USERNAME_DEFAULT
    elif is_legacy_account_name(box_username):
        print(f"  WARNING: users.json box_username '{box_username}' collides with a legacy "
              f"distro system account (cloud-init would adopt it and brick auth) — using "
              f"{BOX_USERNAME_DEFAULT} instead (docs/e2e-testing.md §0).")
        box_username = BOX_USERNAME_DEFAULT
    # 1..8: packet-compiled bundles carry whatever account count the packet publishes (CDE ships 2).
    if not (1 <= len(credlist_usernames) <= 8
            and all(valid_unix_username(n) for n in credlist_usernames)):
        credlist_usernames = list(CREDLIST_USERNAMES_DEFAULT)
    return box_username, credlist_usernames


def competition_name(comp_dir):
# Dashboard seeds run dirs as <run_dir>/comp — a directory-derived name would tag every range "comp-comp".
    path = Path(comp_dir) / "Compfile"
    if not path.exists():
        return Path(comp_dir).name
    name, _, _ = load_compfile(path)
    return name or Path(comp_dir).name


def load_compfile(path):
    name = ""
    scenario = ""
    difficulty = 0

    with open(path) as f:
        for line in f:
            stripped = line.strip()
            if not stripped or " " not in stripped:
                continue
            key, value = stripped.split(" ", 1)
            if key == "name":
                name = value
            elif key == "scenario":
                scenario = value
            elif key == "difficulty":
                try:
                    difficulty = int(value)
                except ValueError:
                    difficulty = 0

    return name, scenario, difficulty


def compfile_flag(path, key, default=0):
    try:
        with open(path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(key + " "):
                    try:
                        return int(stripped.split(" ", 1)[1].strip())
                    except ValueError:
                        return default
    except FileNotFoundError:
        pass
    return default


def compfile_value(path, key, default=""):
    try:
        with open(path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(key + " "):
                    return stripped.split(" ", 1)[1].strip()
    except FileNotFoundError:
        pass
    return default


def stages_red(comp_dir):
    # The private red bridge and the engine's red NIC exist for the life of every range.
    return True


def env_summary():
# Placement and --engine-mgmt-ip move host/address in-process — the ambient env is stale.
    from estate import config
    cfg = config.active()
    mgmt = cfg.engine.mgmt_ip if cfg.is_set("engine.mgmt_ip") else ""
    return (f"endpoint={cfg.proxmox.endpoint or '?'} node={cfg.proxmox.node or '?'} "
            f"datastore={cfg.proxmox.datastore or '?'} "
            f"tpl={cfg.proxmox.template_vm_id or '?'} engine-mgmt={mgmt or 'default'}")


def pick_competition(competitions, label="saved", action="Select a competition"):
    print(f"Found {len(competitions)} {label} competition(s):\n")
    for i, comp in enumerate(competitions, 1):
        name, scenario, difficulty = load_compfile(comps_root() / comp / "Compfile")
        short_scenario = scenario[:80] + ("..." if len(scenario) > 80 else "")
        print(f"  [{i}] {comp}  (difficulty: {difficulty}/10)")
        print(f"       {name}")
        print(f"       {short_scenario}")
        print()

    while True:
        choice = input(f"{action} [1–{len(competitions)}] or 'exit': ").strip()
        if choice.lower() == "exit":
            return None
        try:
            idx = int(choice)
            if 1 <= idx <= len(competitions):
                return competitions[idx - 1]
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {len(competitions)}, or 'exit'.")


