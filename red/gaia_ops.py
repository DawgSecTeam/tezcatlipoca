"""Mythic C2 via Gaia — the competition's C2 box, installed before T0.

The migration plan (§5) and how the club runs it: **every competition installs
and configures its Mythic**, then tlaloc's own `c2-prep` freezes the per-box
payload matrix through the Mythic REST API and the seed plants it. Gaia
(github.com/winterknight1337/Gaia) is the installer/configurator; this module
owns the commands, the credential handoff and the config the agent receives.

Where the C2 box is
    `red.c2_ip` in the Compfile names one. With no `c2_ip` the C2 runs ON red01:
    the VM already exists, the competition's `firewall_dnat` line already points
    the callback port at it (net/firewall_ops.py), and nothing new is
    provisioned — one install step, the same DNAT.

The callback address — the part that decides whether the channel ever works
    A payload built without a callback address inherits Mythic's profile default,
    which on a fresh install is loopback: it compiles, installs, "succeeds" and
    never checks in. The address the team boxes dial is
    `callback_ip_for(comp_dir)` (172.31.<team>.1) on the gateway port the
    Compfile's `firewall_dnat` maps to the C2 box. Both are handed to the agent
    as `c2.callback_host` / `c2.callback_port` (tlaloc passes them through to
    every payload build), and the same pair is what the operator's DNAT must
    expose — with the Mythic http profile LISTENING on that port (the profile
    instance's port), since the range's DNAT translates port-for-port.

Gaia itself
    Its checkout lives on the engine host at `TEZ_GAIA_PATH` (default `~/gaia`),
    the same shape as TEZ_BADAUTO_PATH / TEZ_NAKON_PATH so the paths live in the
    dashboard's `.env`. Every command runs with that checkout as CWD: gaia.py
    reads its `.env` relative to the working directory. Verified against
    gaia.py's argparse:

        install    gaia.py install -S <box> -u <ssh user> -i <key>
                            --install-updates --install-deps --install-mythic
        auth       gaia.py auth -S <box> -P 7443 -u mythic_admin -p -k
                            (-p is a PROMPT: the password goes in on stdin)
        user       gaia.py user create -u <names> --cred-stdout
        operation  gaia.py operation create -n <comp>

    `install` leaves the admin password in `<gaia>/mythic_admin_creds.txt` and
    `auth` leaves the API key in `<gaia>/.env`; both are read back here.

Warn-and-continue, like every other red step: a C2 failure is recorded as a
degradation and the agent runs with `c2.enabled` false — the channel stages then
skip cleanly (logged) instead of taking down a deploy whose range is healthy.
"""

import os
import subprocess
import time
from pathlib import Path

from common.utils import compfile_value, local_python, record_degradation

GAIA_REPO = "https://github.com/winterknight1337/Gaia"
# Pinned at clone time for reproducibility; the resolved commit is recorded in
# the step's result so a scrim names the Gaia revision it validated.
GAIA_REF = "main"
GAIA_PATH_ENV = "TEZ_GAIA_PATH"
DEFAULT_GAIA_PATH = "~/gaia"

MYTHIC_WEB_PORT = 7443
MYTHIC_ADMIN = "mythic_admin"
# The agent's own Mythic account (never mythic_admin: the event runs on an
# account that can be revoked without locking the operator out).
AGENT_USER = "tlaloc"
PASSWORD_ENV = "TLALOC_MYTHIC_PASSWORD"
# Where the pushed password env file lands on red01 (0600, root). tlaloc.service
# reads it via EnvironmentFile so no secret ever rides the pushed config.
ENV_FILE = "/etc/tlaloc/c2.env"

INSTALL_TIMEOUT = 5400          # apt + docker + Mythic images: tens of minutes
SHORT_TIMEOUT = 300
# `auth` right after install can meet a web UI that is up but not yet serving JSON
# (install returns as soon as its steps finish). A transient non-JSON reply is not a
# broken C2, so retry before calling it one.
AUTH_ATTEMPTS = 4
AUTH_RETRY_SECONDS = 30


def gaia_path(env=None):
    """The Gaia checkout on this host (engine host in production)."""
    raw = (env or os.environ).get(GAIA_PATH_ENV) or DEFAULT_GAIA_PATH
    return Path(raw).expanduser()


def venv_python(path):
    """Gaia's own interpreter when its venv exists, else the engine's python.

    Gaia is not importable from the engine's interpreter: its requirements pin
    the `mythic` SDK plus aiohttp and friends. The venv is built once by
    `setup_argv` and every gaia.py invocation uses it.
    """
    candidate = Path(path) / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else local_python()


def gaia_argv(path, *args):
    """`<python> <checkout>/gaia.py <args>` — the checkout is the CWD."""
    return [venv_python(path), str(Path(path) / "gaia.py"), *args]


def setup_argv(path, requirements=None):
    """The two commands that make the checkout runnable."""
    path = Path(path)
    req = Path(requirements) if requirements else path / "requirements.txt"
    venv = path / ".venv"
    return [
        ["python3", "-m", "venv", str(venv)],
        [str(venv / "bin" / "pip"), "install", "-q", "--disable-pip-version-check",
         "-r", str(req)],
    ]


def normalize_requirements(path):
    """A UTF-8 copy of Gaia's requirements file when it is not already UTF-8.

    Upstream ships `requirements.txt` as UTF-16 (a CRLF editor's accident), and
    pip reads requirements files as text — it either errors or installs nothing.
    The normalized copy is what `setup_argv` installs from; the original is left
    alone so `git status` in the checkout stays clean.
    """
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


# --------------------------------------------------------------------------- #
# command builders (pure — the tests assert these, the runner executes them)
# --------------------------------------------------------------------------- #

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
    """`-p` is a store_true prompt in Gaia: the password arrives on stdin."""
    return gaia_argv(path, "auth", "-S", str(server), "-P", str(int(port)),
                     "-u", str(user), "-p", "-k")


def user_argv(path, names, stdout=True):
    argv = gaia_argv(path, "user", "create", "-u", *[str(n) for n in names])
    if stdout:
        argv.append("--cred-stdout")
    return argv


def operation_argv(path, name):
    return gaia_argv(path, "operation", "create", "-n", str(name))


def clone_argv(path, ref=GAIA_REF, repo=GAIA_REPO):
    return ["git", "clone", "--depth", "1", "--branch", str(ref), str(repo),
            str(Path(path))]


# --------------------------------------------------------------------------- #
# reading Gaia's own handoffs
# --------------------------------------------------------------------------- #

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
    """The C2 box's OWN admin password, from /opt/Mythic/.env.

    This is the authority: Mythic stores the password hash in its database, so a
    re-run of the installer — which rewrites `.env` and the host-side handoff file
    but cannot change an existing user — can leave the handoff file stale while the
    box keeps working (live 2026-10-16).
    """
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


# --------------------------------------------------------------------------- #
# the callback address + the agent's config block
# --------------------------------------------------------------------------- #

def callback_port_for(comp_dir, default=None):
    """The gateway port the callback is DNAT'd on.

    `red.c2_callback_port` wins when set (the port the Mythic http profile
    actually listens on — the range's DNAT translates port-for-port, so the
    gateway port and the profile's listening port must be the same number).
    Otherwise the Compfile's `firewall_dnat` supplies it:
    `firewall_dnat 4470->10.0.0.198` means the team gateways expose 4470.
    """
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
    """The callback address a Mythic http profile expects.

    `callback_ip_for()` hands back a bare `172.31.<team>.1`; Mythic's profile
    takes a URL (`http://…`) and the port in its own parameter, so the scheme is
    added here and the port is NEVER baked into the host string.
    """
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
    """The `c2.*` keys the rendered tlaloc config must carry.

    The password is deliberately ABSENT: config carries the env var NAME, the
    secret travels in the 0600 env file (see `env_file_body`).
    """
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


# --------------------------------------------------------------------------- #
# the step
# --------------------------------------------------------------------------- #

def _default_runner(argv, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
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
    """Gaia's venv + its requirements (once per checkout). Returns (ok, detail).

    Gaia imports the `mythic` SDK, aiohttp and friends — none of which the engine
    host's interpreter has — so the checkout gets its own venv and every
    gaia.py call runs on that interpreter.
    """
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


def prepare_mythic(comp_dir, *, box_ip, ssh_user, ssh_key, runner=None,
                   gaia=None, comp_id=None, operation=None, callback_host=None,
                   callback_port=None, agent_user=AGENT_USER, log=print):
    """Install + configure Mythic for one competition. Returns a result dict.

    Order: checkout → install (apt/deps/Mythic) → admin creds → auth (API key) →
    agent user → operation → the `c2.*` block + 0600 env body for the push.

    Every failure lands in `degradations` and returns ok=False with what was
    reached; the caller keeps deploying (the agent then skips the C2 stages).
    """
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
    # 2026-10-16: `POST /auth` answered 403 "Authentication Failed" for the creds
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
    # non-zero (live smoke 2026-10-16: "gaia user create: rver ... -P, --port 7443
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
    if getattr(proc, "returncode", 1) != 0 and not agent_password:
        result["degradations"].append(f"gaia user create: {_tail(proc)}")

    name = operation or comp_id or comp_dir.name
    proc = runner(operation_argv(path, name), cwd=cwd, timeout=SHORT_TIMEOUT)
    result["reached"].append("operation")
    result["gaia_output"]["operation"] = _tail(proc, 300)
    if getattr(proc, "returncode", 1) != 0:
        result["degradations"].append(f"gaia operation create: {_tail(proc)}")

    result["api_key"] = parse_env_value(
        _read(path / ".env"), "MYTHIC_API_KEY")
    result["c2"] = tlaloc_c2_block(comp_dir, server=box_ip, operation=name,
                                   callback_host=callback_host,
                                   callback_port=callback_port, user=agent_user)
    # The agent authenticates with USERNAME/PASSWORD (tlaloc's client posts
    # /auth), so the password that travels is its OWN account's when Gaia handed
    # one back, else the admin password (never logged).
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
    """Record the step's degradations in the run's degradation list."""
    for item in result.get("degradations") or []:
        record_degradation(f"mythic_{where}", str(item)[:400])


# --------------------------------------------------------------------------- #
# handing the configuration to the agent's deploy
# --------------------------------------------------------------------------- #

def apply_c2_to_config(config_path, c2_block):
    """Merge the c2 keys into a rendered tlaloc config (idempotent, in place).

    The deploy renders `.tlaloc-config.yaml` first and pushes it later, so the
    Mythic step has to land its keys in that same file or the agent would be
    configured for a C2 it cannot authenticate to.
    """
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
    """The remote command that installs the 0600 env file on red01.

    Root-owned and 0600: the Mythic password never rides the pushed config (which
    the organizer's diff reads) and never appears in an argument list.
    """
    return (f"sudo -n mkdir -p $(dirname {ENV_FILE}) && "
            f"sudo -n tee {ENV_FILE} >/dev/null && sudo -n chmod 600 {ENV_FILE}")


def push_env(ssh_key, red_ip, body, *, runner=None, timeout=120):
    """Push the env file over SSH (stdin), like the config push."""
    runner = runner or _default_runner
    argv = ["ssh", "-i", str(ssh_key), "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"sysadmin@{red_ip}", env_push_command(body)]
    proc = runner(argv, input=body, timeout=timeout)
    return {"ok": getattr(proc, "returncode", 1) == 0, "detail": _tail(proc)}


def mythic_enabled(comp_dir):
    """Whether this competition installs Mythic (`red.c2 off` opts out).

    Default is ON: the club installs and configures Mythic for every
    competition, and a comp that wants no C2 says so explicitly.
    """
    raw = compfile_value(Path(comp_dir) / "Compfile", "red.c2", "") or ""
    return str(raw).strip().lower() not in ("off", "false", "0", "no")


def c2_plan(comp_dir, *, red_ip=None, comp_id=None):
    """Everything the step needs, derived from the Compfile alone."""
    from red import realm_c2_ops
    return {
        "box": c2_box_for(comp_dir, default_ip=red_ip),
        "callback_host": realm_c2_ops.callback_ip_for(comp_dir),
        "callback_port": callback_port_for(comp_dir),
        "operation": comp_id or Path(comp_dir).name,
    }
