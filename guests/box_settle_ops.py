"""Wait for freshly booted boxes to settle (no apt/dpkg holders) before a plant."""

import os
import shlex
import time

from infra.guest_exec import guest_agent_exec_root, wait_for_guest_agent
from common.ssh_ops import ssh_via_gateway
from common.utils import PRINT_LOCK, record_degradation, run_concurrent


# Match the upgrade WORKER by full command line: `pgrep -x unattended-upgr` also matches
# Ubuntu's permanent unattended-upgrade-shutdown daemon and read every box BUSY forever.
_SETTLE_CHECK = (
    # Non-apt distro: nothing can hold a dpkg lock, but `apt-get check` is rc=127 BUSY forever.
    "command -v apt-get >/dev/null 2>&1 || { echo SETTLED; exit 0; }\n"
    "pgrep -f '/usr/bin/unattended-upgrad[e]( |$)' >/dev/null 2>&1 && echo BUSY\n"
    "apt-get -o DPkg::Lock::Timeout=1 check >/dev/null 2>&1 || echo BUSY\n"
    "echo SETTLED\n"
)


def _box_settled(node, vmid):
# All via the guest agent; SSH is the channel the boot storm starves. None (not False) when the
# channel is alive but its data calls are useless (realm returns NULL for every exec): "cannot
# verify", not "busy" — callers then fall back to the SSH probe.
    try:
        if not wait_for_guest_agent(node, vmid, timeout=20):
            return False
        _rc, out, _err = guest_agent_exec_root(
            node, vmid, _SETTLE_CHECK, timeout=30)
        out = out or ""
        if "SETTLED" in out:
            return "BUSY" not in out
        return None
    except Exception:
        return None


def _box_settled_via_ssh(ctx, t):
    script = _SETTLE_CHECK
    try:
        r = ssh_via_gateway(ctx, t["ip"], f"sudo bash -c {shlex.quote(script)}",
                            timeout=20, user=ctx.get("box_username", "ubuntu"))
    except Exception:
        return False
    return r.returncode == 0 and "SETTLED" in (r.stdout or "") and "BUSY" not in (r.stdout or "")


# `command -v` guard makes a non-cloud-init box an honest no-op; 2>&1 folds cloud-init's stderr
# into the reported tail.
_CLOUD_INIT_CHECK = (
    "command -v cloud-init >/dev/null 2>&1 || { echo 'status: done'; exit 0; }\n"
    "cloud-init status 2>&1\n"
)
_CLOUD_INIT_POLL = 10


def _cloud_init_verdict(rc, out):
    """`cloud-init status` -> 'done', 'error' or 'running'. The text settles the state
    whenever it names one; rc only decides when it says nothing (rc 2 can mean done)."""
    out = out or ""
    if "status: error" in out:
        return "error"
    if "status: done" in out or "status: disabled" in out:
        return "done"
    if "status: running" in out:
        return "running"
    if rc == 2:
        return "error"
    if rc == 0:
        return "done"
    return "running"


# Boot-time package_upgrade can legitimately run long when a node is busy cloning several
# goldens at once; the bound is a knob (TEZ_BOX_CLOUD_INIT_TIMEOUT) so a loaded night does not
# read as a hung box. Default unchanged.
_CLOUD_INIT_TIMEOUT = int(os.environ.get("TEZ_BOX_CLOUD_INIT_TIMEOUT", "600"))


def wait_boxes_cloud_init(ctx, targets, timeout=None, run=None):
    """Bounded wait for cloud-init to finish before a long apt/build session: the base image's
    boot-time `package_upgrade` can bounce sshd mid-session (exit 255) and the apt-holder check
    alone slips through the gaps. 'error' warns and passes; still running at the bound fails."""
    run = run or ssh_via_gateway
    timeout = _CLOUD_INIT_TIMEOUT if timeout is None else timeout
    user = ctx.get("box_username", "ubuntu")
    deadline = time.time() + timeout

    def _one(t):
        ip = t["ip"]
        tail = ""
        while True:
            try:
                r = run(ctx, ip, _CLOUD_INIT_CHECK, timeout=20, user=user)
                verdict = _cloud_init_verdict(r.returncode, r.stdout)
                tail = (r.stdout or "").strip() or tail
            except Exception:
                verdict = "running"
            if verdict == "done":
                with PRINT_LOCK:
                    print(f"    {ip}: cloud-init done")
                return True
            if verdict == "error":
                with PRINT_LOCK:
                    print(f"    WARNING: {ip} cloud-init finished with errors — proceeding: "
                          f"{tail[:150]}")
                record_degradation("cloud-init errored", f"{ip}: {tail[:150]}")
                return True
            if time.time() >= deadline:
                raise RuntimeError(
                    f"cloud-init on {ip} still running after {timeout}s — refusing to open "
                    f"the build session against a box whose boot-time package_upgrade can "
                    f"bounce sshd mid-build (last status: {tail[-300:]})")
            time.sleep(_CLOUD_INIT_POLL)

    results = run_concurrent(targets, _one)
    for r in results:
        if isinstance(r, Exception):
            raise r


def wait_boxes_settled(targets, node, timeout=240, ssh_fallback=None, *,
                       cloud_init_ctx, cloud_init_timeout=None):
# Polls every box until it settles after boot and returns the vmids that never settled (not
# fatal). cloud_init_ctx is keyword-only because a box is not settled until cloud-init is done.
    wait_boxes_cloud_init(cloud_init_ctx, targets, timeout=cloud_init_timeout)
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
