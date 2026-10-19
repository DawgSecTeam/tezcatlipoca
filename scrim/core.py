import contextlib
import json
import os
import re
import time
from pathlib import Path

from common.utils import local_python  # re-exported for scrim/* — see __all__
from config.config_ops import write_text_atomic
from estate import config
__all__ = ["local_python"]
REPO = Path(__file__).resolve().parent.parent
SSH_OPTS = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
# The same two options as argv pairs, for the callers that build an ssh/scp argv list.
SSH_NOCHECK = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
# A blue cycle is a SCOPED visit, not a shift: at 1800 s every run paced itself to its prompt
# and the event collapsed to a couple of TIMED OUT cycles. Real incident work needs 8-10 min,
# so the cap gives a one-action scope headroom; raising it (TEZ_CYCLE_TIMEOUT) trades cycle
# count for cycles that can finish.
CYCLE_TIMEOUT = int(config.active().knobs.cycle_timeout or 600)
CYCLE_TARGET_PERIOD = 480
# 60 s: at 300 s a blue restoring in 2-5 min left every DOWN->UP between samples, so
# restorations/max-simultaneous-down graded on nothing.
MONITOR_INTERVAL = 60


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def write_evidence(path, text):
    """Write an evidence file atomically at 0600 — they name every scored service on the range."""
    return write_text_atomic(path, text, mode=0o600)


def secure_evidence(path):
    """chmod an evidence file some other tool copied in (scp/cp/shutil) to 0600."""
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
    return config.active().engine.username


def is_local_endpoint(base_url):
    """Table-driven in scrim/llm_endpoints.py; unknown hosts stay operator-side."""
    from scrim import llm_endpoints          # local: core is imported BY scrim/endpoints
    return llm_endpoints.is_operator_side(base_url)


def read_comp_json(comp, name):
    return json.loads((Path(comp) / name).read_text())


def read_state(comp):
    """The competition's flat state view, through the engine's own reader."""
    from estate import state as estate_state
    return estate_state.load_flat(comp)


def is_windows_box(box):
    return "windows" in str(box.get("template") or "")


def engine_ip_from(comp):
    """The engine's IP, as written into `<comp>/credentials.txt` by the deploy."""
    return re.search(r"http://([0-9.]+)", (Path(comp) / "credentials.txt").read_text()).group(1)


def engine_proxy(key, user, engine):
    return f"ssh -i {key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -W %h:%p {user}@{engine}"


def box_ssh_base(creds, connect_timeout=None):
    """ssh argv prefix reaching team boxes over the engine jump (host appended by caller)."""
    base = ["ssh", "-i", creds["KEY_PATH"], *SSH_NOCHECK]
    if connect_timeout:
        base += ["-o", f"ConnectTimeout={connect_timeout}"]
    proxy = engine_proxy(creds["KEY_PATH"], creds["VM_USER"], creds["ENGINE_IP"])
    return base + ["-o", f"ProxyCommand={proxy}"]


def ssh_key_path():
    """The configured absolute path, falling back to the checkout's key — and that fallback is
    only right for a manual checkout: under a central run the engine is a per-run worktree with
    no key in it, so a hardcoded checkout path reads a file that is not there.
    """
    raw = (config.active().ssh.private_key_path or "").strip()
    if not raw:
        return str((REPO / "proxmox").resolve())
    return str(config.ssh_key_path())
