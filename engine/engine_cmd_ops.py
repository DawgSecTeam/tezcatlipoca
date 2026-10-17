"""The one SSH-to-engine command runner every engine step goes through."""

import subprocess

from common.ssh_ops import engine_ssh_opts


def _run_engine_cmd(ctx, cmd, step, check=True, timeout=60, capture=False, input=None):
# `input` (bytes, e.g. a tarball for `sudo tar xzf -`) feeds remote stdin; captured output then stays bytes.
    argv = ["ssh", "-i", ctx["ssh_key_path"], *engine_ssh_opts(ctx),
            f"{ctx['vm_username']}@{ctx['scoring_engine_ip']}", cmd]
    try:
        return subprocess.run(argv, check=check, timeout=timeout, input=input,
                              capture_output=capture, text=capture and input is None)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"engine step {step!r} failed: {e}") from e
