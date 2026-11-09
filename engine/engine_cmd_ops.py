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


_BUILD_ATTEMPTS = 8
# The lab's uplink delivers ~15s of connectivity then drops for tens of seconds, repeatedly
# (sampled 2026-11-08: 6/10 short calls succeed, in runs). A build needs to outlast several
# outages, and each retry keeps the layers/images the failed attempt already pulled, so the
# delays grow instead of repeating: ~20s,30s,45s,60s,90s,120s,120s.
_BUILD_RETRY_DELAYS = (20, 30, 45, 60, 90, 120, 120)


def _retry_over_flaky_uplink(ctx, cmd, *, step, timeout, first_flags="", attempts=None):
    """Run a network-bound step until it survives a flap.

    The lab's WAN drops for tens of seconds at a time; a build or clone that needs a few
    continuous minutes of egress otherwise dies on a blip and costs the whole deploy
    (run-2ef1679d: "go mod download" timed out, and the same request answered 200 later).
    The retries drop first_flags so work the failed attempt finished is reused.
    """
    attempts = attempts or _BUILD_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            return _run_engine_cmd(ctx, cmd + (first_flags if attempt == 1 else ""),
                                   timeout=timeout, step=step)
        except RuntimeError:
            if attempt == attempts:
                raise
            delay = _BUILD_RETRY_DELAYS[min(attempt - 1, len(_BUILD_RETRY_DELAYS) - 1)]
            print(f"  {step} failed (attempt {attempt}/{attempts}) — the uplink is flaky; "
                  f"retrying in {delay}s")
            time.sleep(delay)


def start_quotient(ctx, *, attempts=3, timeout=600):
    """`docker compose up -d` with the fresh-volume Postgres race folded in.

    Two copies of this retry had drifted (the build path and the template path); both are
    here now. A loaded node's initdb misses compose's health window and the server exits 2
    once, so one failure is not a dead engine (live: run-e3d569d1f, run-e5fe9b87).
    """
    last = None
    # `compose build` builds only the app images; the third-party services (postgres, redis)
    # come from a registry, and on a flapping uplink compose's implicit pull gives up and `up`
    # then dies with "No such image: postgres:17" (live: run-9070c600, which had built the
    # whole app). Pull them explicitly first, with the same retries the build uses.
    # --ignore-buildable: `server` declares `image: quotient-server` AND a `build:` stanza, so a
    # bare pull asks a registry for an image that only ever exists after a local build and dies
    # with "pull access denied for quotient-server" (live: run-7b648ae3), killing every deploy.
    _retry_over_flaky_uplink(ctx,
                             "cd /opt/quotient && sudo docker compose pull --ignore-buildable",
                             step="compose pull", timeout=1800)
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
