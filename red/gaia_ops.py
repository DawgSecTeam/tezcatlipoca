"""Mythic C2 via Gaia — the competition's C2 box, installed before T0."""

import contextlib
import fcntl
import os
import re
import subprocess
import time
from pathlib import Path

from common.utils import compfile_value, local_python, record_degradation

GAIA_REPO = "https://github.com/winterknight1337/Gaia"
# A moving branch, not a pin: the step records the resolved revision it validated
GAIA_REF = "main"
DEFAULT_GAIA_PATH = "~/gaia"

MYTHIC_WEB_PORT = 7443
MYTHIC_ADMIN = "mythic_admin"
# Never mythic_admin: revocable without locking the operator out
AGENT_USER = "tlaloc"
PASSWORD_ENV = "TLALOC_MYTHIC_PASSWORD"
# 0600 root; tlaloc.service reads it via EnvironmentFile — no secret in the pushed config
ENV_FILE = "/etc/tlaloc/c2.env"

INSTALL_TIMEOUT = 5400          # apt + docker + Mythic images: tens of minutes
SHORT_TIMEOUT = 300
# right after install the web UI can be up but not yet serving JSON; a transient
# non-JSON reply is not a broken C2, so retry before calling it one
AUTH_ATTEMPTS = 4
AUTH_RETRY_SECONDS = 30


def gaia_path():
    from estate import config
    raw = config.active().knobs.gaia_path or DEFAULT_GAIA_PATH
    return Path(raw).expanduser()


def venv_python(path):
    """Gaia's venv when present, else the engine python: its pins (mythic SDK, aiohttp)
    are not on the engine interpreter. setup_argv builds the venv once."""
    candidate = Path(path) / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else local_python()


def gaia_argv(path, *args):
    """gaia.py reads its .env relative to CWD, so every caller sets cwd=<checkout>."""
    return [venv_python(path), str(Path(path) / "gaia.py"), *args]


def setup_argv(path, requirements=None):
    """The two commands that make the checkout runnable: venv + requirements."""
    path = Path(path)
    req = Path(requirements) if requirements else path / "requirements.txt"
    venv = path / ".venv"
    return [
        ["python3", "-m", "venv", str(venv)],
        [str(venv / "bin" / "pip"), "install", "-q", "--disable-pip-version-check",
         "-r", str(req)],
    ]


def normalize_requirements(path):
    """Upstream ships requirements.txt as UTF-16, which pip misreads. Normalize a copy,
    leave the original so the checkout's git status stays clean."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError:                       # no file: nothing to normalise
        return path
    if not raw.startswith(b"\xff\xfe") and not raw.startswith(b"\xfe\xff"):
        return path
    text = raw.decode("utf-16")
    out = path.with_suffix(".utf8.txt")
    out.write_text(text, encoding="utf-8")
    return out



def install_argv(path, server, user, key=None, port=22, updates=True, deps=True,
                 mythic=True):
    argv = gaia_argv(path, "install", "-S", str(server), "-u", str(user),
                     "-P", str(int(port)))
    if key:
        argv += ["-i", str(key)]
    if updates:
        argv.append("--install-updates")
    if deps:
        argv.append("--install-deps")
    if mythic:
        argv.append("--install-mythic")
    return argv


def auth_argv(path, server, user=MYTHIC_ADMIN, port=MYTHIC_WEB_PORT):
    """-p is a PROMPT: the password arrives on stdin. No -k: in this Gaia it means
    ssl=False (http) and Mythic serves TLS on 7443; the https client already skips
    certificate verification."""
    return gaia_argv(path, "auth", "-S", str(server), "-P", str(int(port)),
                     "-u", str(user), "-p")


def user_argv(path, names, stdout=True):
    argv = gaia_argv(path, "user", "create", "-u", *[str(n) for n in names])
    if stdout:
        argv.append("--cred-stdout")
    return argv


def operation_argv(path, name):
    return gaia_argv(path, "operation", "create", "-n", str(name))


def assign_argv(path, operation, users):
    return gaia_argv(path, "operation", "assign", "-o", str(operation),
                     "-u", *[str(u) for u in users])


def mythic_answering(box_ip, runner, timeout=8):
    """A resume must not re-run the installer: it rebuilds every image (15+ min) and
    racing another install can wedge the stack."""
    try:
        proc = runner(["curl", "-sk", "-m", str(timeout), "-o", "/dev/null",
                       "-w", "%{http_code}", f"https://{box_ip}:7443/health"],
                      timeout=timeout + 2)
    except Exception:                                   # noqa: BLE001 - probe only
        return False
    return getattr(proc, "stdout", "").strip() == "200"


def clone_argv(path, ref=GAIA_REF, repo=GAIA_REPO):
    return ["git", "clone", "--depth", "1", "--branch", str(ref), str(repo),
            str(Path(path))]



def parse_admin_password(text):
    """`mythic_admin:<pw>` (what install leaves in mythic_admin_creds.txt)."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith(f"{MYTHIC_ADMIN}:"):
            value = line.split(":", 1)[1].strip()
            if value:
                return value
    return None


def box_admin_password(box_ip, ssh_user, ssh_key, *, runner=None, timeout=60):
    """Authority: Mythic stores the password hash in its DB, so an installer re-run
    rewrites .env and the handoff file but cannot change an existing user — the file
    can be stale while the box works."""
    if not box_ip or not ssh_key:
        return None
    argv = ["ssh", "-i", str(ssh_key), "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
            f"{ssh_user}@{box_ip}",
            "sudo -n grep -E '^MYTHIC_ADMIN_PASSWORD=' /opt/Mythic/.env 2>/dev/null "
            "| head -1 | cut -d= -f2- | tr -d '\"'"]
    proc = (runner or _default_runner)(argv, timeout=timeout)
    if getattr(proc, "returncode", 1) != 0:
        return None
    value = (getattr(proc, "stdout", "") or "").strip()
    return value or None


def rewrite_admin_password(creds_path, password):
    """Put the password the C2 actually accepts into the handoff file."""
    creds_path = Path(creds_path)
    try:
        lines = creds_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{MYTHIC_ADMIN}:"):
            lines[index] = f"{MYTHIC_ADMIN}:{password}"
            replaced = True
    if not replaced:
        lines.append(f"{MYTHIC_ADMIN}:{password}")
    try:
        creds_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def parse_env_value(text, key):
    """A `KEY=value` line out of Gaia's .env (quotes stripped)."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


def env_file_body(password, host=None, port=MYTHIC_WEB_PORT, user=AGENT_USER):
    """The 0600 env file red01's unit reads (no secret in the pushed config)."""
    lines = [f"# pushed by tezcatlipoca at deploy time (0600, root)",
             f"{PASSWORD_ENV}={password}"]
    if host:
        lines.append(f"MYTHIC_HOST={host}")
        lines.append(f"MYTHIC_PORT={int(port)}")
        lines.append(f"MYTHIC_USER={user}")
    return "\n".join(lines) + "\n"



def callback_port_for(comp_dir, default=None):
    """`red.c2_callback_port` wins (the profile's listening port: the range DNATs
    port-for-port, so gateway and profile ports must match); else the Compfile's
    `firewall_dnat` left side."""
    comp = Path(comp_dir) / "Compfile"
    explicit = str(compfile_value(comp, "red.c2_callback_port", "") or "").strip()
    if explicit.isdigit():
        return int(explicit)
    raw = compfile_value(comp, "firewall_dnat", "") or ""
    for spec in str(raw).split(","):
        spec = spec.strip()
        if "->" not in spec:
            continue
        gw_port = spec.split("->", 1)[0].strip()
        if gw_port.isdigit():
            return int(gw_port)
    return default


def c2_box_for(comp_dir, default_ip=None):
    """The C2 box: `red.c2_ip` in the Compfile, else red01."""
    raw = compfile_value(Path(comp_dir) / "Compfile", "red.c2_ip", "") or ""
    return str(raw).strip() or default_ip


def callback_url(host, port=None):
    """Mythic profiles take a URL with the port in its own parameter: the scheme is
    added here and the port is NEVER baked into the host string."""
    host = str(host or "").strip()
    if not host:
        return None
    if "://" not in host:
        host = f"http://{host}"
    scheme, _, rest = host.partition("://")
    bare = rest.split("/")[0]
    if ":" in bare:                      # a port in the host belongs in its own field
        bare = bare.split(":", 1)[0]
    return f"{scheme}://{bare}"


def tlaloc_c2_block(comp_dir, *, server, operation, callback_host, callback_port,
                    user=AGENT_USER):
    """The `c2.*` keys. Password deliberately ABSENT: config carries the env var NAME."""
    block = {
        "enabled": True,
        "base_url": f"https://{server}:{MYTHIC_WEB_PORT}",
        "username": user,
        "password_env": PASSWORD_ENV,
        "operation": operation,
        "verify_tls": False,
    }
    if callback_host:
        block["callback_host"] = callback_url(callback_host)
    if callback_port:
        block["callback_port"] = int(callback_port)
    return block



# Gaia is third-party: never leak the Proxmox token or dashboard bearers into it
_STRIPPED_ENV = ("TEZ_PROXMOX_API_TOKEN", "TEZ_OPS_TOKEN", "TEZ_DASHBOARD_TOKENS")


def _default_runner(argv, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    if "env" not in kwargs:
        kwargs["env"] = {k: v for k, v in os.environ.items()
                         if k not in _STRIPPED_ENV}
    return subprocess.run(argv, **kwargs)


def _tail(proc, n=300):
    return ((getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or ""))[-n:]


def ensure_checkout(path, *, runner=None, ref=GAIA_REF, repo=GAIA_REPO):
    """The Gaia checkout, cloned at `ref` if missing. Returns (ok, detail)."""
    path = Path(path)
    if (path / "gaia.py").exists():
        return True, "present"
    runner = runner or _default_runner
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = runner(clone_argv(path, ref=ref, repo=repo), timeout=SHORT_TIMEOUT)
    if getattr(proc, "returncode", 1) != 0:
        return False, f"git clone failed: {_tail(proc)}"
    if not (path / "gaia.py").exists():
        return False, "clone produced no gaia.py"
    return True, "cloned"


def ensure_setup(path, *, runner=None, timeout=900):
    """Gaia's venv + requirements, once per checkout. Returns (ok, detail)."""
    path = Path(path)
    if not (path / "gaia.py").exists():
        return False, "no checkout"
    if (path / ".venv" / "bin" / "python").exists():
        return True, "present"
    if not (path / "requirements.txt").exists():
        return False, "no requirements.txt in the checkout"
    runner = runner or _default_runner
    argv_venv, argv_pip = setup_argv(
        path, requirements=normalize_requirements(path / "requirements.txt"))
    for argv in (argv_venv, argv_pip):
        proc = runner(argv, cwd=str(path), timeout=timeout)
        if getattr(proc, "returncode", 1) != 0:
            return False, f"{' '.join(argv[:4])}: {_tail(proc)}"
    return True, "created"


def checkout_commit(path, runner=None):
    runner = runner or _default_runner
    try:
        proc = runner(["git", "-C", str(path), "rev-parse", "HEAD"], timeout=60)
    except Exception:                                       # noqa: BLE001
        return None
    return (getattr(proc, "stdout", "") or "").strip() or None


def gaia_lock_path(box_ip):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(box_ip))
    return Path.home() / ".tezcatlipoca" / "locks" / f"gaia-{safe}.lock"


@contextlib.contextmanager
def mythic_lock(box_ip):
    """Serialize one C2 box's install/configure sequence across processes.

    flock is released by the kernel on close/exit, so a failed or killed
    installer cannot leave a stale lock behind.
    """
    path = gaia_lock_path(box_ip)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def prepare_mythic(comp_dir, *, box_ip, ssh_user, ssh_key, runner=None,
                   gaia=None, comp_id=None, operation=None, callback_host=None,
                   callback_port=None, agent_user=AGENT_USER, log=print):
    """Install + configure Mythic: checkout → install → admin creds → auth → user →
    operation → c2 block + env body. Every failure lands in `degradations` with
    ok=False; the caller keeps deploying and the agent skips the C2 stages."""
    comp_dir = Path(comp_dir)
    runner = runner or _default_runner
    path = Path(gaia) if gaia else gaia_path()
    result = {"ok": False, "box": str(box_ip), "gaia": str(path), "commit": None,
              "reached": [], "degradations": [], "c2": None, "env": None,
              "admin_password": None, "api_key": None,
              "gaia_output": {}, "environment": _environment(sshd=True),
              "callback": {"host": callback_host, "port": callback_port}}
    if not box_ip:
        result["degradations"].append("no C2 box: red.c2_ip unset and red01 unknown")
        return result

    ok, detail = ensure_checkout(path, runner=runner)
    if not ok:
        result["degradations"].append(f"gaia checkout: {detail}")
        return result
    result["commit"] = checkout_commit(path, runner=runner)

    ok, detail = ensure_setup(path, runner=runner)
    if not ok:
        result["degradations"].append(f"gaia setup: {detail}")
        return result

    cwd = str(path)
    # The pre-lock probe is the cheap resume check; the post-lock one is the race
    # guard: a second process must re-decide after waiting, not install again.
    answering = mythic_answering(box_ip, runner)
    with mythic_lock(box_ip):
        if not answering and mythic_answering(box_ip, runner):
            answering = True
        if answering:
            result["reached"].append("install (already running)")
            result["gaia_output"]["install"] = "skipped: Mythic answered /health"
        else:
            proc = runner(install_argv(path, box_ip, ssh_user, key=ssh_key),
                          cwd=cwd, timeout=INSTALL_TIMEOUT)
            result["reached"].append("install")
            result["gaia_output"]["install"] = _tail(proc, 2000)
            if getattr(proc, "returncode", 1) != 0:
                result["degradations"].append(f"gaia install: {_tail(proc)}")
                return result

        creds = path / "mythic_admin_creds.txt"
        try:
            file_password = parse_admin_password(creds.read_text(encoding="utf-8"))
        except OSError:
            file_password = None

        # The C2 box's own .env is the authority on its admin password. An install re-run
        # against a Mythic that already has a database leaves the passwords disagreeing —
        # and the handoff file lands on THIS host, so it can be the stale one. Live
        # caught live: `POST /auth` answered 403 "Authentication Failed" for the creds
        # file's password while the box's MYTHIC_ADMIN_PASSWORD returned 200 + an
        # access_token, with the whole stack healthy — the step blamed a C2 that worked.
        candidates = []
        if file_password:
            candidates.append(("mythic_admin_creds.txt", file_password))
        env_password = box_admin_password(box_ip, ssh_user, ssh_key, runner=runner)
        if env_password and env_password != file_password:
            candidates.append(("the C2 box's /opt/Mythic/.env", env_password))
        if not candidates:
            result["degradations"].append(
                "no admin password: install left no creds and the box has no "
                "MYTHIC_ADMIN_PASSWORD")
            return result

        # AUTH FIRST — this is what unlocks everything after it. `user create` and
        # `operation create` speak to Mythic through the API key `auth` writes into
        # Gaia's .env; run either before it and Gaia prints the auth usage and exits
        # non-zero (caught live: "gaia user create: rver ... -P, --port 7443
        # ... -u, --user mythic_admin" was that usage dump, not an argument error).
        authed_with = None
        for source, candidate in candidates:
            for attempt in range(AUTH_ATTEMPTS):
                proc = runner(auth_argv(path, box_ip), cwd=cwd, timeout=SHORT_TIMEOUT,
                              input=f"{candidate}\n")
                if getattr(proc, "returncode", 1) == 0:
                    authed_with = (source, candidate)
                    break
                if attempt + 1 < AUTH_ATTEMPTS:
                    time.sleep(AUTH_RETRY_SECONDS)
            if authed_with:
                break
        result["reached"].append("auth")
        result["gaia_output"]["auth"] = _tail(proc, 300)
        if not authed_with:
            result["degradations"].append(f"gaia auth: {_tail(proc)}")
        else:
            result["admin_password"] = authed_with[1]
            if authed_with[0] != "mythic_admin_creds.txt":
                result["password_source"] = authed_with[0]
                # Do not hand the organizer a password the C2 rejects.
                if rewrite_admin_password(creds, authed_with[1]) and log:
                    log(f"  mythic      : admin password came from {authed_with[0]} — "
                        f"mythic_admin_creds.txt corrected")

        # The agent's own account, now that the key exists. Gaia prints
        # "user:password"; and re-running for a competition that already has it is not
        # an error (the account exists).
        proc = runner(user_argv(path, [agent_user]), cwd=cwd, timeout=SHORT_TIMEOUT,
                      input="")
        result["reached"].append("user")
        result["gaia_output"]["user"] = _tail(proc, 300)
        agent_password = _password_from_creds(getattr(proc, "stdout", ""), agent_user)
        # The operator's password is what c2-prep (and the agent) authenticate with; an
        # admin-password stand-in logs in as nobody we created, and surfaced only as a 403
        # from /auth. Gaia rotates it on every run, so no password means this step failed.
        if not agent_password:
            result["degradations"].append(
                f"gaia user create returned no password for {agent_user}: {_tail(proc)}")

        name = operation or comp_id or comp_dir.name
        proc = runner(operation_argv(path, name), cwd=cwd, timeout=SHORT_TIMEOUT)
        result["reached"].append("operation")
        result["gaia_output"]["operation"] = _tail(proc, 300)
        if getattr(proc, "returncode", 1) != 0:
            result["degradations"].append(f"gaia operation create: {_tail(proc)}")

        # An operator outside the operation sees a Hasura schema without the actions (no
        # createPayload), which surfaced only as "field not found in mutation_root".
        proc = runner(assign_argv(path, name, [agent_user]), cwd=cwd,
                      timeout=SHORT_TIMEOUT)
        result["reached"].append("assign")
        result["gaia_output"]["assign"] = _tail(proc, 300)
        if getattr(proc, "returncode", 1) != 0:
            result["degradations"].append(f"gaia operation assign: {_tail(proc)}")

    result["api_key"] = parse_env_value(
        _read(path / ".env"), "MYTHIC_API_KEY")
    result["c2"] = tlaloc_c2_block(comp_dir, server=box_ip, operation=name,
                                   callback_host=callback_host,
                                   callback_port=callback_port, user=agent_user)
    # the agent posts /auth with its OWN account's password, else the admin's (never logged)
    result["env"] = env_file_body(agent_password or result.get("admin_password"), host=box_ip,
                                 user=agent_user)
    result["environment"] = _environment(sshd=True, mythic=True)
    result["ok"] = not result["degradations"]
    if log:
        log(f"  mythic      : gaia {'ok' if result['ok'] else 'DEGRADED'} on "
            f"{box_ip} — reached {', '.join(result['reached']) or 'nothing'}, "
            f"callback {callback_host}:{callback_port}")
    return result


def _password_from_creds(text, user):
    for line in (text or "").splitlines():
        line = line.strip().strip("{}\"'")
        if line.startswith(f"{user}:"):
            value = line.split(":", 1)[1].strip().strip("'\"")
            if value:
                return value
    return None


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _environment(*, sshd=False, mythic=False):
    """What the step needs from the environment (recorded, not guessed)."""
    return {"sshd": bool(sshd), "mythic": bool(mythic),
            "gaia_path": str(gaia_path())}


def degrade(result, where):
    for item in result.get("degradations") or []:
        record_degradation(f"mythic_{where}", str(item)[:400])



def apply_c2_to_config(config_path, c2_block):
    """Merge the c2 keys in place: the deploy renders the config first and pushes later,
    so the Mythic step must land in that same file."""
    import yaml
    path = Path(config_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = data.setdefault("c2", {})
    if not isinstance(section, dict):                       # a malformed file is a bug
        section = {}
        data["c2"] = section
    section.update(c2_block or {})
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def env_push_command(body):
    """Installs the 0600 root env file; the password rides stdin, never an argument list."""
    return (f"sudo -n mkdir -p $(dirname {ENV_FILE}) && "
            f"sudo -n tee {ENV_FILE} >/dev/null && sudo -n chmod 600 {ENV_FILE}")


def push_env(ssh_key, red_ip, body, *, runner=None, timeout=120):
    runner = runner or _default_runner
    argv = ["ssh", "-i", str(ssh_key), "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"sysadmin@{red_ip}", env_push_command(body)]
    proc = runner(argv, input=body, timeout=timeout)
    return {"ok": getattr(proc, "returncode", 1) == 0, "detail": _tail(proc)}


def mythic_enabled(comp_dir):
    """Default ON: every competition gets a Mythic; a comp that wants none says so."""
    raw = compfile_value(Path(comp_dir) / "Compfile", "red.c2", "") or ""
    return str(raw).strip().lower() not in ("off", "false", "0", "no")


def _team_records(comp_dir):
    import json
    try:
        teams = json.loads((Path(comp_dir) / "teams.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [tv for tv in teams.values() if isinstance(tv, dict)]


def _in_path_firewall(comp_dir):
    import json
    try:
        boxes = json.loads((Path(comp_dir) / "boxes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    # boxes.json is a list of box records; accept the older name-keyed object too.
    if isinstance(boxes, dict):
        boxes = boxes.values()
    return any(box.get("in_path") for box in boxes if isinstance(box, dict))


def callback_host_for(comp_dir):
    """The address team boxes dial for the C2 on this topology.

    It is the team GATEWAY address (192.168.<id>.1) in both topologies, because
    that is the address the range DNATs to red01: without an in-path firewall the
    engine owns it and DNATs the callback port; with one, the generated pfSense
    config carries the same DNAT (\"firewall owns the gateway address\"). The
    engine's transit address (172.31.<id>.1) is NOT the C2: it is Quotient, and a
    payload built with it beacons into the scoring engine and never checks in.
    """
    identifiers = [tv.get("identifier") for tv in _team_records(comp_dir)
                   if tv.get("identifier") is not None]
    if not identifiers:
        return None
    return f"192.168.{identifiers[0]}.1"


def c2_plan(comp_dir, *, red_ip=None, comp_id=None):
    """Everything the step needs, derived from the Compfile and the topology."""
    return {
        "box": c2_box_for(comp_dir, default_ip=red_ip),
        "callback_host": callback_host_for(comp_dir),
        "callback_port": callback_port_for(comp_dir),
        "operation": comp_id or Path(comp_dir).name,
    }
