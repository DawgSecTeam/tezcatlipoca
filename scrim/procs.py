import contextlib
import os
import signal
import subprocess
import threading

from scrim.core import log


# Grace between the SIGINT that lets a child release its resources and the SIGKILL that
# sweeps whatever ignored it.
TREE_STOP_GRACE = 30


# subclasses TimeoutExpired so the handlers around the blue cycles and the watchdog keep
# catching it; __str__ names the recovery path because a timed-out deploy/verify is
# resumed, not restarted (--skip-deploy/--from-phase, or --resume-event past T0)
class ScrimTimeout(subprocess.TimeoutExpired):

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


# fed from a thread because communicate(input=...) cannot be re-entered after
# TimeoutExpired (exactly what the kill-and-drain path needs); the pipe is closed
# immediately — an open stdin pipe keeps a straggler grandchild alive
def _feed_stdin(proc, text):
    try:
        proc.stdin.write(text)
        proc.stdin.close()
    except (BrokenPipeError, ValueError, OSError):
        pass


# SIGINT first, so a child can release what it holds; SIGKILL because the grandchildren
# this exists to reap may ignore SIGINT. killpg on an already-empty group is a no-op.
def _kill_tree(proc, grace=TREE_STOP_GRACE):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGINT)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


# own session so a timeout kills the whole tree, not just the direct child: subprocess.run
# SIGKILLs the DIRECT child only, after which create-competition.py/badauto/their
# nakon/ssh grandchildren survive with the deploy lock held and keep mutating
# infrastructure (one hung opencode cycle held the shared lock ~80 min because a
# grandchild outlived the kill). SIGINT to the group, escalate to SIGKILL after `grace`.
def run_tree(cmd, cwd=None, env=None, timeout=None, check=True, tail=None,
             stdin_text=None, grace=TREE_STOP_GRACE):
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


# every long-running call must go through the supervised process tree
def run(cmd, cwd=None, env=None, timeout=None, check=True, tail=None):
    return run_tree(cmd, cwd=cwd, env=env, timeout=timeout, check=check, tail=tail)


# the password goes on stdin — a running command's argv is world-readable via ps, and
# interpolating a generated password into the remote shell string is one shell
# metacharacter from a syntax error aborting the fire test; same pin every other box
# path uses (beacon_ops._ssh_box). The script follows the password on the same stdin
# stream, which is what `sudo -S -p '' bash -s` expects.
def box_sudo_stdin(base, host, box_pw, script):
    return list(base) + [host, "sudo -S -p '' bash -s"], box_pw + "\n" + script
