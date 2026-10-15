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
BAD_AUTO = REPO.parent / "bad-auto"
SSH_OPTS = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
# The same two options as argv pairs, for the callers that build an ssh/scp argv list.
SSH_NOCHECK = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
# A blue cycle is a SCOPED visit, not a shift: the old 1800 s budget let each
# opencode run pace to its prompt's "25 wall-clock minutes" and the whole event
# collapsed to two TIMED OUT cycles (run3 2026-10-11: 0 of 2 cycles rc=0 against
# the gates' >= 8). 600 s hard cap + a prompt scoped to ONE change keeps the
# cadence honest: measured on the GX10 qwen-flash (run4 2026-10-12), a cycle
# with real incident work needs 8-10 min — 540 killed 5 of 6 — so the cap gives
# the one-action scope headroom while the target period keeps ~6 starts/hour.
# TEZ_CYCLE_TIMEOUT overrides it for a slower LLM endpoint: on the only reachable
# server at the 20261013d run (qwen3.8-flash-next, ~3 min per 10-token reply) every
# 600 s cycle timed out and blue never engaged at all. Raising the budget trades
# cycle count for cycles that can actually finish.
CYCLE_TIMEOUT = int(config.active().knobs.cycle_timeout or 600)
CYCLE_TARGET_PERIOD = 480
# Board samples every minute: at 300 s a blue restoring in 2-5 min (live run3)
# left every DOWN->UP transition between samples — the scoreboard never showed a
# down state, and restorations/max-simultaneous-down graded on nothing.
MONITOR_INTERVAL = 60


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
    return config.active().engine.username


def is_local_endpoint(base_url):
    """True for an operator-side LLM endpoint: slower, key-less, tunnelled.

    Table-driven since 2026-10-14 (`scrim/llm_endpoints.py`). The old body was
    `"openrouter" not in base_url`, so every public API that was not openrouter.com — the
    official DeepSeek API included — was classified operator-side and driven as if it were a
    local llama.cpp (literal `local` as blue's apiKey, the slow-local red profile, a reverse
    tunnel pointed at a TLS port). Unknown hosts stay operator-side: that is what local
    rehearsals rely on.
    """
    from scrim import llm_endpoints          # local: core is imported BY scrim/endpoints
    return llm_endpoints.is_operator_side(base_url)


def read_comp_json(comp, name):
    """Parse `<comp>/<name>` (boxes.json, box_services.json, estate-state.json, ...)."""
    return json.loads((Path(comp) / name).read_text())


def read_state(comp):
    """The competition's flat state view, through the engine's own reader.

    v0.3.0 writes ONE state document and stopped writing `.deploy_state.json`, so the scrim
    side's direct reads of that file died with FileNotFoundError on the first estate range
    (live 2026-10-15, run-75347fca, scrim/cli -> blue_agent.stage_blues). `estate.state`
    still falls back to the legacy file read-only, so a pre-v0.3 range keeps working.
    """
    from estate import state as estate_state
    return estate_state.load_flat(comp)


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


def ssh_key_path():
    """The ssh key this RANGE was built with: the configured absolute path when there is one.

    The configured key path is what the deploy itself resolved (estate/config.py) and
    what a run host configures (e.g. /home/hna/tez-proxmox). The `<engine>/proxmox` fallback is
    only right for a manual checkout, where the key really sits in the tree: under a central run
    the engine is a per-run WORKTREE with no key in it, so anything that hardcodes a checkout path
    reads a file that is not there (2026-10-14: red staging resolved a key against a sibling
    checkout and died AFTER the range was already built).
    """
    raw = (config.active().ssh.private_key_path or "").strip()
    if not raw:
        return str((REPO / "proxmox").resolve())
    return str(config.ssh_key_path())
