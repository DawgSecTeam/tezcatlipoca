"""Connection context: terraform agent_context, ssh key resolution, and the two SSH hops (gateway, engine)."""

import json
import os
import subprocess
from pathlib import Path

from ssh_ops import engine_ssh_opts, gateway_proxy
from utils import BOX_USERNAME_DEFAULT, load_users_config

from verifier.model import CheckError

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"


def read_terraform_ctx(comp_dir):
    """Read agent_context from `terraform output -json` in the competition's own
    per-comp state (competitions/<id>/terraform); resolve ssh_key_path absolute."""
    tf_dir = Path(comp_dir) / "terraform"
    try:
        raw = subprocess.run(
            ["terraform", "output", "-json"],
            cwd=str(tf_dir),
            capture_output=True, text=True, check=True,
        ).stdout
    except FileNotFoundError:
        raise CheckError("terraform not found on PATH — pass --engine-ip to skip Terraform.")
    except subprocess.CalledProcessError as e:
        raise CheckError(f"`terraform output -json` failed: {e.stderr.strip() or e}")
    try:
        ctx = json.loads(json.loads(raw)["agent_context"]["value"])
    except (json.JSONDecodeError, KeyError) as e:
        raise CheckError(f"could not parse agent_context from terraform output: {e}")
    key_path = ctx.get("ssh_key_path")
    if key_path and not os.path.isabs(key_path):
        ctx["ssh_key_path"] = str((tf_dir / key_path).resolve())
    return ctx


def resolve_ssh_key():
    """Resolve TF_VAR_ssh_private_key_path from .env, trying repo-root and terraform-relative."""
    val = os.environ.get("TF_VAR_ssh_private_key_path")
    if not val:
        return None
    for base in (REPO_ROOT, REPO_ROOT / "terraform"):
        cand = (base / val).resolve()
        if cand.exists():
            return str(cand)
    return str((REPO_ROOT / val).resolve())


def build_ctx(args, comp_dir):
    """Assemble connection context, honoring --engine-ip override."""
    ctx = {}
    tf_error = None
    if not args.engine_ip:
        ctx = read_terraform_ctx(comp_dir)
    else:
        try:
            ctx = read_terraform_ctx(comp_dir)
        except CheckError as e:
            tf_error = e
    if args.engine_ip:
        ctx["scoring_engine_ip"] = args.engine_ip
    if not ctx.get("scoring_engine_ip"):
        raise CheckError("no scoring_engine_ip (Terraform gave none and no --engine-ip).")
    if not ctx.get("ssh_key_path"):
        ctx["ssh_key_path"] = resolve_ssh_key()
    if not ctx.get("vm_username"):
        ctx["vm_username"] = os.environ.get("TF_VAR_vm_username")
    if not ctx.get("box_username"):
        ctx["box_username"] = load_users_config(comp_dir)[0] or BOX_USERNAME_DEFAULT
    if tf_error:
        print(f"  (note: Terraform context unavailable, using .env: {tf_error})")
    return ctx


# The ssh options every verify hop shares (order matters to callers that assert argv).
_SSH_COMMON_OPTS = ("-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "ConnectTimeout=10")


def red_ssh_argv(ctx, red_user, red_ip, remote_cmd):
    """argv for an ssh straight to red01 (its mgmt address — no gateway hop)."""
    return ["ssh", "-i", ctx["ssh_key_path"], *_SSH_COMMON_OPTS,
            f"{red_user}@{red_ip}", remote_cmd]


def ssh_via_gateway(ctx, target_ip, cmd, timeout=60):
    """SSH to a target box via the scoring-engine gateway (mirrors create-competition.py)."""
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
    """SSH directly to the scoring engine itself (no gateway hop — it's the gateway)."""
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
