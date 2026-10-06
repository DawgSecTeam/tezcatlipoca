"""Wait for freshly booted boxes to settle (no apt/dpkg holders) before a plant."""

import shlex
import time

from range_ops import guest_agent_exec_root, wait_for_guest_agent
from ssh_ops import ssh_via_gateway
from utils import run_concurrent


# Match the upgrade WORKER by full command line. `pgrep -x unattended-upgr` (the
# 15-char truncated comm) ALSO matches Ubuntu's permanent unattended-upgrade-shutdown
# --wait-for-signal daemon, so every Ubuntu box read BUSY forever and each prep_apt
# pass burned the full settle budget (winad-testrun 2026-09-25). The [e] keeps the
# pattern from matching this script's own shell.
_SETTLE_CHECK = (
    # Non-apt distro: nothing can hold a dpkg lock, so it is settled by definition —
    # without this guard `apt-get check` is rc=127 BUSY forever and every such box
    # burns the full 240s settle budget per pass (live-found 2026-09-27).
    "command -v apt-get >/dev/null 2>&1 || { echo SETTLED; exit 0; }\n"
    "pgrep -f '/usr/bin/unattended-upgrad[e]( |$)' >/dev/null 2>&1 && echo BUSY\n"
    "apt-get -o DPkg::Lock::Timeout=1 check >/dev/null 2>&1 || echo BUSY\n"
    "echo SETTLED\n"
)


def _box_settled(node, vmid):
    """True once a freshly booted box has settled: guest agent responds, no
    unattended-upgrades process, dpkg lock free. All via the guest agent (root,
    virtio-serial) — SSH is exactly the channel the boot storm starves. The lock check
    is `apt-get check` with a 1s lock timeout rather than `fuser`: same condition, no
    psmisc dependency on minimal cloud images.

    Returns None (not False) when the agent channel is alive but its data calls are
    useless — the realm node answers pings and returns NULL for every exec result, so
    "no SETTLED seen" there means 'cannot verify', not 'busy'. Treating that as False
    burned the whole 240s settle budget per prep_apt pass on realm (2026-09-25);
    callers fall back to the SSH probe on None."""
    try:
        if not wait_for_guest_agent(node, vmid, timeout=20):
            return False
        _rc, out, _err = guest_agent_exec_root(
            node, vmid, _SETTLE_CHECK, timeout=30)
        out = out or ""
        if "SETTLED" in out:
            return "BUSY" not in out
        return None  # agent ping ok, data channel dead (realm) — let the caller probe SSH
    except Exception:
        return None


def _box_settled_via_ssh(ctx, t):
    """SSH fallback of the settle check, for nodes whose guest agent won't return data."""
    script = _SETTLE_CHECK
    try:
        r = ssh_via_gateway(ctx, t["ip"], f"sudo bash -c {shlex.quote(script)}",
                            timeout=20, user=ctx.get("box_username", "ubuntu"))
    except Exception:
        return False
    return r.returncode == 0 and "SETTLED" in (r.stdout or "") and "BUSY" not in (r.stdout or "")


def wait_boxes_settled(targets, node, timeout=240, ssh_fallback=None):
    """Poll every box until it settles after boot — the real condition the old blind
    150s sleep waited out (the initial unattended-upgrades run starves both the guest
    agent and the ssh -W forward). Boxes poll concurrently, each with the full budget.
    Returns the vmids that never settled; that is not fatal — the caller's per-box
    retry ladder covers a stubborn box exactly as before."""
    deadline = time.time() + timeout

    def _wait(t):
        while time.time() < deadline:
            settled = _box_settled(node, t["vmid"])
            if settled is True:
                return True
            if settled is None and ssh_fallback is not None and ssh_fallback(t):
                return True
            time.sleep(10)
        return False

    results = run_concurrent(targets, _wait)
    return {t["vmid"] for t, r in zip(targets, results) if r is not True}
