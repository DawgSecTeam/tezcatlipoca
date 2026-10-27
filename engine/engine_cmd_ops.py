"""The one SSH-to-engine command runner every engine step goes through."""

import subprocess
import time

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


def start_quotient(ctx, *, attempts=3, timeout=600):
    """`docker compose up -d` with the fresh-volume Postgres race folded in.

    Two copies of this retry had drifted (the build path and the template path); both are
    here now. A loaded node's initdb misses compose's health window and the server exits 2
    once, so one failure is not a dead engine (live: run-e3d569d1f, run-e5fe9b87).
    """
    last = None
    for attempt in range(attempts):
        try:
            _run_engine_cmd(ctx, "cd /opt/quotient && sudo docker compose up -d",
                            timeout=timeout, step="compose up")
            return
        except Exception as e:                              # noqa: BLE001 - the DB health window is a race
            last = e
            print(f"    compose up attempt {attempt + 1} failed ({e}); retrying")
            time.sleep(15)
    raise last
