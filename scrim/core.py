import contextlib
import json
import os
import re
import time
from pathlib import Path

from config_ops import write_text_atomic
REPO = Path(__file__).resolve().parent.parent
BAD_AUTO = REPO.parent / "bad-auto"
SSH_OPTS = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
# The same two options as argv pairs, for the callers that build an ssh/scp argv list.
SSH_NOCHECK = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
CYCLE_TIMEOUT = 1800
CYCLE_TARGET_PERIOD = 600
MONITOR_INTERVAL = 300


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def write_evidence(path, text):
    """Write an evidence file atomically, 0600.

    Evidence files were written with a plain write_text at the process umask: the final
    scoreboard and the per-team service dumps name every scored service and every error
    string on the range, yet a report reader on a shared host could read them — and a torn
    write during teardown was possible (audit find D5).
    """
    return write_text_atomic(path, text, mode=0o600)


def secure_evidence(path):
    """chmod an evidence file that some other tool copied in (scp/cp/shutil) to 0600."""
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return path


def shutil_copy(src, dst):
    """Copy bytes and return the destination (so callers can chmod what they just wrote)."""
    dst.write_bytes(src.read_bytes())
    return dst


def now_iso(ts=None):
    """Local wall-clock `YYYY-mm-ddTHH:MM:SS` (now, or `ts` epoch seconds)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def vm_username():
    """The provisioning user every ssh path to the engine/boxes/red01 logs in as."""
    return os.environ.get("TF_VAR_vm_username", "sysadmin")


def bad_auto_deploy_default(field, fallback=None):
    """bad-auto's built-in default for `deploy.<field>`.

    Teardown has to assert the vmid `badauto destroy` will act on, and none of the
    run's own sources always carry it: `--red-vmid` is optional, the harness-written
    config.yaml omits it, and a manifest recorded before this change has null. The
    default in bad-auto's source is what its CLI actually falls back to, so read it
    from there rather than guessing."""
    import re as _re
    try:
        text = (BAD_AUTO / "badauto" / "config.py").read_text(encoding="utf-8")
    except OSError:
        return fallback
    m = _re.search(rf'"{_re.escape(field)}":\s*(\d+)', text)
    return int(m.group(1)) if m else fallback


def is_local_endpoint(base_url):
    """True for an operator-side (non-openrouter) LLM endpoint: slower, key-less, tunnelled."""
    return "openrouter" not in base_url


def read_comp_json(comp, name):
    """Parse `<comp>/<name>` (boxes.json, box_services.json, .deploy_state.json, ...)."""
    return json.loads((Path(comp) / name).read_text())


def is_windows_box(box):
    """A boxes.json entry whose template name contains "windows" (password auth, Administrator)."""
    return "windows" in str(box.get("template") or "")


def engine_ip_from(comp):
    """The engine's IP, as written into `<comp>/credentials.txt` by the deploy."""
    return re.search(r"http://([0-9.]+)", (Path(comp) / "credentials.txt").read_text()).group(1)


def engine_proxy(key, user, engine):
    """The `ssh -W` jump command that reaches a team subnet through the engine."""
    return f"ssh -i {key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -W %h:%p {user}@{engine}"


def box_ssh_base(creds, connect_timeout=None):
    """ssh argv prefix that reaches team boxes over the engine jump (host appended by caller)."""
    base = ["ssh", "-i", creds["KEY_PATH"], *SSH_NOCHECK]
    if connect_timeout:
        base += ["-o", f"ConnectTimeout={connect_timeout}"]
    proxy = engine_proxy(creds["KEY_PATH"], creds["VM_USER"], creds["ENGINE_IP"])
    return base + ["-o", f"ProxyCommand={proxy}"]
