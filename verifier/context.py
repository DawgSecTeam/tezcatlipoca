"""Connection context: the engine's coordinates from the state document, ssh key
resolution, and the two SSH hops (gateway, engine)."""

import os
import subprocess
from pathlib import Path

from common.ssh_ops import engine_ssh_opts, gateway_proxy
from common.utils import BOX_USERNAME_DEFAULT, load_users_config
from estate import config as estate_config, state as estate_state

from verifier.model import CheckError
from estate import config

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"


def read_estate_ctx(comp_dir):
# The engine's coordinates from the competition's own state document.
    state = estate_state.read_flat(comp_dir)
    if state is None:
        raise CheckError(
            f"no state document at {estate_state.state_path(comp_dir)} — this workspace "
            "holds no deploy state; verify the range's own workspace (the one its deploy "
            "ran in), or pass --engine-ip to skip it.")
    return {"scoring_engine_ip": state.get("engine_mgmt_ip") or "",
            "ssh_key_path": str(estate_config.ssh_key_path()),
            "vm_username": estate_config.active().engine.username}


def resolve_ssh_key():
# Resolve the configured SSH private key, falling back to the legacy env name.
    val = config.active().ssh.private_key_path
    if not val:
        return str(estate_config.ssh_key_path())
    return str((REPO_ROOT / val).resolve() if not os.path.isabs(val) else Path(val))


def build_ctx(args, comp_dir):
# Assemble connection context, honoring --engine-ip override.
    ctx = {}
    estate_error = None
    try:
        ctx = read_estate_ctx(comp_dir)
    except CheckError as e:
        estate_error = e
    if args.engine_ip:
        ctx["scoring_engine_ip"] = args.engine_ip
    if not ctx.get("scoring_engine_ip"):
        raise CheckError("no scoring_engine_ip (the state document gave none and no "
                         "--engine-ip was passed).")
    if not ctx.get("ssh_key_path"):
        ctx["ssh_key_path"] = resolve_ssh_key()
    if not ctx.get("vm_username"):
        ctx["vm_username"] = estate_config.active().engine.username
    if not ctx.get("box_username"):
        ctx["box_username"] = load_users_config(comp_dir)[0] or BOX_USERNAME_DEFAULT
    if estate_error:
        print(f"  (note: state document unavailable, using .env: {estate_error})")
    return ctx


# The ssh options every verify hop shares (order matters to callers that assert argv).
_SSH_COMMON_OPTS = ("-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "ConnectTimeout=10")


def red_ssh_argv(ctx, red_user, red_ip, remote_cmd):
# argv for an ssh straight to red01 (its mgmt address — no gateway hop).
    return ["ssh", "-i", ctx["ssh_key_path"], *_SSH_COMMON_OPTS,
            f"{red_user}@{red_ip}", remote_cmd]


def ssh_via_gateway(ctx, target_ip, cmd, timeout=60):
# SSH to a target box via the scoring-engine gateway (mirrors create-competition.py).
    key = ctx["ssh_key_path"]
    scoring_user = ctx["vm_username"]
    box_username = ctx.get("box_username", BOX_USERNAME_DEFAULT)
    if not key or not scoring_user:
        raise CheckError("missing SSH key path or vm_username for gateway SSH.")
    try:
        return subprocess.run(
            [
                "ssh", "-i", key,
                *_SSH_COMMON_OPTS,
                "-o", f"ProxyCommand={gateway_proxy(ctx)}",
                f"{box_username}@{target_ip}", cmd,
            ],
            capture_output=True, text=True, timeout=timeout,
        )
    except OSError as e:
        raise CheckError(f"failed to run ssh: {e}")


def ssh_to_engine(ctx, cmd, timeout=30):
# SSH directly to the scoring engine itself (no gateway hop — it's the gateway).
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    if not key or not scoring_user:
        raise CheckError("missing SSH key path or vm_username for engine SSH.")
    try:
        return subprocess.run(
            ["ssh", "-i", key, *engine_ssh_opts(ctx),
             f"{scoring_user}@{scoring_ip}", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
    except OSError as e:
        raise CheckError(f"failed to run ssh: {e}")
