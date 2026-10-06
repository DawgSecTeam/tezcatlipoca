import contextlib
import os
import signal
import subprocess
import threading

from scrim.core import log


# Grace between the SIGINT that lets terraform release its state lock and the SIGKILL
# that sweeps whatever ignored it (utils.run_terraform uses the same two-phase stop).
TREE_STOP_GRACE = 30


class ScrimTimeout(subprocess.TimeoutExpired):
    """A supervised command blew its wall-clock budget and its process TREE was killed.

    Subclasses TimeoutExpired so the handlers that already exist around the blue cycles
    and the watchdog keep catching it. The message names the recovery path because a
    timed-out deploy/verify is resumed, not restarted: `--skip-deploy`/`--from-phase N`
    for the pipeline, `--resume-event` for an event that already reached T0.
    """

    def __init__(self, cmd, timeout):
        super().__init__(cmd, timeout)
        self.cmd, self.timeout = cmd, timeout

    def __str__(self):
        return (f"command timed out after {self.timeout}s (its whole process group was "
                f"signalled SIGINT then SIGKILL, so no orphaned grandchild still holds a "
                f"lock or keeps mutating infrastructure): "
                f"{' '.join(str(c) for c in self.cmd[:6])}"
                + (" ..." if len(self.cmd) > 6 else "")
                + " — resume the run with --skip-deploy/--from-phase (or --resume-event "
                  "if T0 was already recorded)")


def _feed_stdin(proc, text):
    """Write stdin from a thread so a timeout can still kill and drain the process.

    communicate(input=...) cannot be re-entered after TimeoutExpired, which is exactly
    what the kill-and-drain path needs, so the pipe is fed out of band and closed
    immediately (an open stdin pipe keeps a straggler grandchild alive).
    """
    try:
        proc.stdin.write(text)
        proc.stdin.close()
    except (BrokenPipeError, ValueError, OSError):
        pass


def _kill_tree(proc, grace=TREE_STOP_GRACE):
    """SIGINT the process group, wait, then SIGKILL it and everything left inside it.

    SIGINT first because it is the graceful stop terraform uses to release its state
    lock (utils.run_terraform does the same); SIGKILL because the grandchildren this
    exists to reap may ignore SIGINT. killpg on an already-empty group is a no-op.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGINT)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def run_tree(cmd, cwd=None, env=None, timeout=None, check=True, tail=None,
             stdin_text=None, grace=TREE_STOP_GRACE):
    """Run cmd in its own session so a timeout kills the whole tree, not just the child.

    subprocess.run(timeout=) sends SIGKILL to the DIRECT child only. Every long-running
    command here (create-competition.py, badauto, their terraform/nakon/ssh grandchildren)
    then survives with the deploy lock still held and keeps mutating infrastructure — the
    failure class the file already fixed once for _opencode_run, where one hung cycle held
    the shared lock ~80 min because a grandchild outlived the kill. Modelled on
    utils.run_terraform: SIGINT to the group, escalate to SIGKILL after `grace`.

    Returns CompletedProcess; on timeout raises ScrimTimeout (a TimeoutExpired) whose
    message names the resume path. `run` below is the thin house wrapper for it.
    """
    cmd = [str(c) for c in cmd]
    log("$ " + " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, text=True,
                            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    if stdin_text is not None:
        threading.Thread(target=_feed_stdin, args=(proc, stdin_text), daemon=True).start()
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc, grace)
        proc.communicate()
        raise ScrimTimeout(cmd, timeout) from None
    except BaseException:
        # Ctrl-C / any other driver-side abort must not orphan the tree either.
        _kill_tree(proc, grace)
        proc.communicate()
        raise
    r = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if tail and r.stdout:
        print("\n".join(r.stdout.splitlines()[-tail:]))
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed rc={r.returncode}: {(r.stderr or '')[-800:]}")
    return r


def run(cmd, cwd=None, env=None, timeout=None, check=True, tail=None):
    """Supervised `subprocess.run`: every long-running call goes through the process tree."""
    return run_tree(cmd, cwd=cwd, env=env, timeout=timeout, check=check, tail=tail)


def box_sudo_stdin(base, host, box_pw, script):
    """(ssh argv, stdin text) for a root command on a team box.

    The password goes on stdin — the process argv of a running command is world-readable
    via ps, and interpolating a generated password into the remote shell string is one
    shell metacharacter away from a syntax error aborting the fire test. Same pin every
    other box path uses (beacon_ops._ssh_box). The script follows the password on the
    same stdin stream, which is what `sudo -S -p '' bash -s` expects.
    """
    return list(base) + [host, "sudo -S -p '' bash -s"], box_pw + "\n" + script
