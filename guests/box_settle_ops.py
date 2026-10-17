"""Wait for freshly booted boxes to settle (no apt/dpkg holders) before a plant."""

import shlex
import time

from infra.guest_exec import guest_agent_exec_root, wait_for_guest_agent
from common.ssh_ops import ssh_via_gateway
from common.utils import PRINT_LOCK, record_degradation, run_concurrent


# Match the upgrade WORKER by full command line. `pgrep -x unattended-upgr` (the
# 15-char truncated comm) ALSO matches Ubuntu's permanent unattended-upgrade-shutdown
# --wait-for-signal daemon, so every Ubuntu box read BUSY forever and each prep_apt
# pass burned the full settle budget (measured on a Windows/AD test run). The [e] keeps the
# pattern from matching this script's own shell.
_SETTLE_CHECK = (
    # Non-apt distro: nothing can hold a dpkg lock, so it is settled by definition —
    # without this guard `apt-get check` is rc=127 BUSY forever and every such box
    # burns the full 240s settle budget per pass (caught live).
    "command -v apt-get >/dev/null 2>&1 || { echo SETTLED; exit 0; }\n"
    "pgrep -f '/usr/bin/unattended-upgrad[e]( |$)' >/dev/null 2>&1 && echo BUSY\n"
    "apt-get -o DPkg::Lock::Timeout=1 check >/dev/null 2>&1 || echo BUSY\n"
    "echo SETTLED\n"
)


def _box_settled(node, vmid):
# True once a freshly booted box has settled: guest agent responds, no
#     unattended-upgrades process, dpkg lock free. All via the guest agent (root,
#     virtio-serial) — SSH is exactly the channel the boot storm starves. The lock check
#     is `apt-get check` with a 1s lock timeout rather than `fuser`: same condition, no
#     psmisc dependency on minimal cloud images.
#
#     Returns None (not False) when the agent channel is alive but its data calls are
#     useless — the realm node answers pings and returns NULL for every exec result, so
#     "no SETTLED seen" there means 'cannot verify', not 'busy'. Treating that as False
#     burned the whole 240s settle budget per prep_apt pass on realm ;
#     callers fall back to the SSH probe on None.
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
# SSH fallback of the settle check, for nodes whose guest agent won't return data.
    script = _SETTLE_CHECK
    try:
        r = ssh_via_gateway(ctx, t["ip"], f"sudo bash -c {shlex.quote(script)}",
                            timeout=20, user=ctx.get("box_username", "ubuntu"))
    except Exception:
        return False
    return r.returncode == 0 and "SETTLED" in (r.stdout or "") and "BUSY" not in (r.stdout or "")


# cloud-init status: `command -v` guard makes a non-cloud-init box an honest no-op
# (else `cloud-init: command not found` reads 'running' and burns the whole bound);
# 2>&1 folds cloud-init's own stderr into the tail we report on a stuck box.
_CLOUD_INIT_CHECK = (
    "command -v cloud-init >/dev/null 2>&1 || { echo 'status: done'; exit 0; }\n"
    "cloud-init status 2>&1\n"
)
_CLOUD_INIT_POLL = 10


def _cloud_init_verdict(rc, out):
    """`cloud-init status` -> 'done', 'error' or 'running'.

    The TEXT settles the state whenever it names one, and the exit code only decides
    when the text says nothing. rc 2 alone is not an error verdict: cloud-init returns
    it for a boot that finished with recorded errors, so on an image that always has one
    (this estate's Ubuntu 24.04) rc 2 arrived alongside `status: done` on every box and
    `status: running` while the boot was still going. Reading rc first made the gate
    (a) record a tolerated failure for a box whose boot was over — which hard-fails
    verify's degradations gate — and (b) skip the wait it exists to perform
    (caught live: 'cloud-init errored: <ip>: status: done')."""
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


def wait_boxes_cloud_init(ctx, targets, timeout=600, run=None):
    """Bounded wait for cloud-init to finish on freshly cloned boxes before a long
    apt/build session opens. The base image's boot-time `package_upgrade` (a
    kernel+firmware batch is enough) argues with the build's own apt chain over
    the SAME long ssh session, and when the storm bounces sshd mid-session the
    driver's single ssh call drops with exit 255 (live: engine-template builds
    died 1-2 min into `install engine packages`). The apt-holder settle check
    alone can slip through a gap between cloud-init's apt invocations, so the
    real precondition is cloud-init's own 'done'. A box that reached 'error' is
    warned about and passed — never block a deploy forever on a broken boot;
    a box still running at the bound fails loudly with the status tail, because
    opening the build anyway is exactly the race this closes."""
    run = run or ssh_via_gateway
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
                       cloud_init_ctx, cloud_init_timeout=600):
# Poll every box until it settles after boot — the real condition the old blind
#     150s sleep waited out (the initial unattended-upgrades run starves both the guest
#     agent and the ssh -W forward). Boxes poll concurrently, each with the full budget.
#     Returns the vmids that never settled; that is not fatal — the caller's per-box
#     retry ladder covers a stubborn box exactly as before.
#
#     cloud_init_ctx is required (keyword-only): a freshly cloned box is not settled
#     until cloud-init itself is done, and every consumer of this waiter must carry
#     that guarantee before it opens its own long apt/build session.
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
