"""The one SSH-to-engine command runner every engine step goes through."""

import subprocess

from ssh_ops import engine_ssh_opts


def _run_engine_cmd(ctx, cmd, step, check=True, timeout=60, capture=False, input=None):
# One SSH command to the scoring engine — the shared body of every bootstrap step.
#
#     `step` names the pipeline step this command belongs to. Every command here runs
#     the same `ssh ... <cmd>` shape, so a raw CalledProcessError/TimeoutExpired out of
#     this function names nothing an operator can act on: bootstrap_scoring_engine alone
#     is ~11 steps over up to 1800s, and the only enclosing handler is deploy's generic
#     per-phase message. Raise a labelled RuntimeError instead, chaining the original as
#     __cause__ so the traceback still carries the ssh argv and return code.
#
#     `input` (bytes) is fed to the remote command's stdin — e.g. a tarball for
#     `sudo tar xzf - -C <dir>` (portal_ops.deploy_portal); captured output then stays bytes.
    argv = ["ssh", "-i", ctx["ssh_key_path"], *engine_ssh_opts(ctx),
            f"{ctx['vm_username']}@{ctx['scoring_engine_ip']}", cmd]
    try:
        return subprocess.run(argv, check=check, timeout=timeout, input=input,
                              capture_output=capture, text=capture and input is None)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"engine step {step!r} failed: {e}") from e
