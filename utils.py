import contextlib
import json
import os
import re
import secrets as _secrets
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_USERNAME_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_COMP_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")

PRINT_LOCK = threading.Lock()
MAX_CONCURRENCY = 8


def mint_run_id():
    """Per-deploy run identity: `run-` + 8 hex chars, minted once per competition
    directory and reused by every later deploy/resume from it (constants.ownership_tags).

    The comp dir is per-worktree, so two concurrent deploys of the SAME competition ID
    mint different ids and can no longer destroy each other's VMs — destruction paths
    require the full ownership tag set including this tag (2026-10-02 near-miss)."""
    return f"run-{_secrets.token_hex(4)}"


def run_concurrent(items, fn, max_workers=MAX_CONCURRENCY):
    """Run fn(item) for every item on a bounded thread pool.

    M2.1's shared helper for per-box work. Returns a LIST aligned with `items`: each
    slot holds fn's return value, or the exception fn raised — never raised here — so
    each caller keeps its own aggregate semantics (all-fail abort vs warn-and-continue).
    Items need not be hashable (they're target dicts). max_workers is capped at
    MAX_CONCURRENCY (8), which stays far under the engine's raised sshd MaxSessions
    (64), so the ControlMaster channel is never the bottleneck; the ceiling also keeps
    the Proxmox API poll load bounded. Output interleaving is the caller's problem:
    wrap prints in PRINT_LOCK."""
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
    """True for a box the pipeline must not plant/configure/score/domain-join — e.g. an
    in-path pfSense firewall (FreeBSD): no nakon plant, no golden, no repair/fix_services,
    no cloud-init identity, no scored check, no domain role. It is cloned straight from its
    own template with operator-specified NICs. Kept in boxes.json so per-box-index vmids
    stay positional; every plant/config/scoring path skips it via this predicate."""
    return bool(box.get("unmanaged"))


def valid_unix_username(name):
    """Safe as a remote-shell token, useradd name, and sudoers filename."""
    return bool(name) and _USERNAME_RE.fullmatch(name) is not None


# Legacy distro system accounts (uid<=100 on the Debian and Fedora families): cloud-init
# adopts a colliding name instead of creating it, so the sudoers rule and SSH key land on
# a nologin root-homed shadow and every ssh <name>@box fails no matter what
# (distro-matrix-2026-09-27: box_username operator). Alpine lacks most of these, which is
# why the collision never surfaced on the ubuntu/debian lineup.
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
    """True when the name collides with a legacy distro system account — auth setup
    bricks on the distros that carry it, so both users.json paths reject the name."""
    return name in LEGACY_ACCOUNT_NAMES


def valid_comp_name(name):
    """Confines the competitions/<name> path — no traversal, no shell metachars."""
    return bool(name) and ".." not in name and _COMP_NAME_RE.fullmatch(name) is not None

DNS_FIX_CMD = (
    'printf "nameserver 8.8.8.8\\n" | sudo tee /etc/resolv.conf; '
    'printf "nameserver 8.8.8.8\\n" | sudo tee /etc/resolv.conf.head; '
    "sudo mkdir -p /etc/systemd/resolved.conf.d; "
    'printf "[Resolve]\\nDNS=8.8.8.8\\n" | sudo tee /etc/systemd/resolved.conf.d/upstream.conf; '
    "sudo systemctl restart systemd-resolved 2>/dev/null || true"
)

DNS_FIX_CMD_ROOT = DNS_FIX_CMD.replace("sudo ", "")


BOX_USERNAME_DEFAULT = "ubuntu"
CREDLIST_USERNAMES_DEFAULT = ["admin", "user1", "user2"]


def load_users_config(comp_dir):
    """Load themeable box login + credlist usernames from users.json, or fall back to defaults."""

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
    # 1..8 (was exactly 3): packet-compiled bundles carry whatever accounts the packet
    # publishes (CDE ships 2). fix_services_on_boxes iterates the pairs, so the count
    # was never load-bearing — only the interactive prompt kept the 3.
    if not (1 <= len(credlist_usernames) <= 8
            and all(valid_unix_username(n) for n in credlist_usernames)):
        credlist_usernames = list(CREDLIST_USERNAMES_DEFAULT)
    return box_username, credlist_usernames


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
    """Read an integer Compfile knob; default when the key or the file is absent."""
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
    """Read a string Compfile knob (e.g. `quotient_ref <sha-or-tag>`); default when
    the key or the file is absent."""
    try:
        with open(path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(key + " "):
                    return stripped.split(" ", 1)[1].strip()
    except FileNotFoundError:
        pass
    return default


def pick_competition(competitions, label="saved", action="Select a competition"):
    print(f"Found {len(competitions)} {label} competition(s):\n")
    for i, comp in enumerate(competitions, 1):
        name, scenario, difficulty = load_compfile(f"competitions/{comp}/Compfile")
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


def run_terraform(args, cwd, env=None, timeout=None, check=True, grace=60):
    """Run terraform in its own process group so a driver interruption can't orphan it.

    A plain subprocess.run leaves `terraform apply` running as a grandchild holding the
    state lock and still mutating infra if the Python driver is killed (winad-testrun
    2026-09-25). Here SIGINT/SIGTERM/timeout/Ctrl-C all forward SIGINT to the group
    (terraform's graceful stop: finishes in-flight ops, releases the lock), escalating to
    SIGKILL after `grace` seconds. SIGKILL of the driver itself is not catchable.
    Returns CompletedProcess (returncode only); raises CalledProcessError when check."""
    cmd = ["terraform", *args]
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, start_new_session=True)

    def _stop():
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        # terraform reaps its own provider plugins before exiting; sweep any straggler
        # left in the group so nothing keeps mutating infra behind a released lock.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)

    prev = None
    if threading.current_thread() is threading.main_thread():
        def _on_term(signum, frame):
            raise KeyboardInterrupt(f"signal {signum}")
        prev = signal.signal(signal.SIGTERM, _on_term)
    try:
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop()
            raise
        except BaseException:
            _stop()
            raise
    finally:
        if prev is not None:
            signal.signal(signal.SIGTERM, prev)
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)
    return subprocess.CompletedProcess(cmd, rc)
