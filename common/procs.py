"""Supervised child processes: own session, whole-tree kill, stdin feeding.

Shared by the deploy (trail wipe) and the scrim harness; it used to live under scrim/, which
made a production deploy import the test harness.
"""

import contextlib
import os
import signal
import subprocess
import threading




def _log(msg):
    import time
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# Grace between the SIGINT that lets a child release its resources and the SIGKILL that sweeps it.
TREE_STOP_GRACE = 30


# Subclasses TimeoutExpired so the handlers around the blue cycles and the watchdog keep catching it.
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


# Threaded because communicate(input=...) cannot be re-entered after TimeoutExpired; stdin
# closes immediately — an open pipe keeps a straggler grandchild alive.
def _kill_tree(proc, grace=TREE_STOP_GRACE):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGINT)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def _reap(proc):
    """Drain a killed child without trusting Popen's post-timeout internals.

    `proc.communicate()` a SECOND time after TimeoutExpired raises AttributeError on
    Python 3.13 ('Popen' object has no attribute '_fileobj2output'): the timeout path
    leaves the pipe bookkeeping cleared, so the documented "call communicate() again"
    recipe is a crash there — and it replaced the real timeout with that AttributeError
    (run-700f7719, stage_verify). Read the pipes directly once the tree is dead.
    """
    out, err = [], []
    for stream, sink in ((proc.stdout, out), (proc.stderr, err)):
        if stream is None:
            continue
        try:
            sink.append(stream.read() or "")
        except (OSError, ValueError):
            pass
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)
    return "".join(out), "".join(err)


# Own session so a timeout reaps the whole tree: subprocess.run kills only the direct child,
# leaving create-competition/nakon/ssh grandchildren alive and holding the deploy lock.
def run_tree(cmd, cwd=None, env=None, timeout=None, check=True, tail=None,
             stdin_text=None, grace=TREE_STOP_GRACE):
    cmd = [str(c) for c in cmd]
    _log("$ " + " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, text=True,
                            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    # stdin goes through communicate(input=...), never a feeder thread: the thread
    # closes the pipe and communicate() then flushes it before reading, which raises
    # "ValueError: I/O operation on closed file" on 3.13 (run-ea2ddde7, the fire
    # test's sudo call). communicate's own writer thread owns the whole lifecycle.
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc, grace)
        out, err = _reap(proc)
        raise ScrimTimeout(cmd, timeout) from None
    except BaseException:
        # A driver-side abort (Ctrl-C) must not orphan the tree either.
        _kill_tree(proc, grace)
        out, err = _reap(proc)
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


# Password on stdin: argv is world-readable via ps. `sudo -S -p '' bash -s` takes the script
# after the password on the same stream.
def box_sudo_stdin(base, host, box_pw, script):
    return list(base) + [host, "sudo -S -p '' bash -s"], box_pw + "\n" + script
