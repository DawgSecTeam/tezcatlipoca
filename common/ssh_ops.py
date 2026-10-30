"""SSH helpers via the scoring-engine gateway and the state document's engine context."""

import subprocess
import time
from pathlib import Path

import requests

from infra.guest_exec import diagnose_unreachable_box, wait_for_guest_agent
from common.utils import PRINT_LOCK, record_degradation, run_concurrent
from common.windows_ops import is_windows_template
from estate import config

DEFAULT_KNOWN_HOSTS = str(Path.home() / ".tezcatlipoca" / "known_hosts")


def _engine_opts(known_hosts=None, host=None):
# accept-new pins the engine key on first connect and rejects a changed key afterwards (TOFU).
    kh = known_hosts or DEFAULT_KNOWN_HOSTS
    Path(kh).parent.mkdir(parents=True, exist_ok=True)
    opts = ["-o", f"UserKnownHostsFile={kh}", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
            # A silently dropped node link otherwise hangs until the caller's full timeout.
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
    if host:
        # %C hashes (user, host, port) into a fixed-length token: the old `cm-<ip>-22-%r` was
        # a Unix socket path that overflowed UNIX_PATH_MAX on a deep $HOME.
        opts += ["-o", "ControlMaster=auto",
                 "-o", f"ControlPath={Path(kh).parent / 'cm-%C'}",
                 "-o", "ControlPersist=900"]
    return opts


def engine_ssh_opts(ctx):
    return _engine_opts(ctx.get("known_hosts"), host=ctx.get("scoring_engine_ip"))


def gateway_proxy(ctx):
    import shlex
    # The outer ssh percent-expands a ProxyCommand before the shell sees it, and OpenSSH 10
    # makes an escape its ProxyCommand table lacks fatal — %C belongs to ControlPath only. Left
    # single, every box hop died in ~10ms with rc 255 and empty stdout, which the settle gate
    # read as "cloud-init still running". Doubling it hands the inner ssh a literal %C, so its
    # socket hash (and the multiplexing) is unchanged.
    opts = " ".join(shlex.quote(part) for part in engine_ssh_opts(ctx)).replace("%", "%%")
    return (f"ssh -i {shlex.quote(str(ctx['ssh_key_path']))} {opts} -W %h:%p "
            f"{ctx['vm_username']}@{ctx['scoring_engine_ip']}")


def box_ssh_opts(proxy, *, extra_opts=()):
# The box key rotates on clone/rebuild/rollback, so the box hop is unverified; it is tunnelled
# inside the host-key-pinned engine channel.
    return ["-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-o", f"ProxyCommand={proxy}", *extra_opts]


def box_ssh_argv(ctx, target_ip, user, remote=None, *, extra_opts=()):
    argv = ["ssh", "-i", ctx["ssh_key_path"],
            *box_ssh_opts(gateway_proxy(ctx), extra_opts=extra_opts),
            f"{user}@{target_ip}"]
    if remote is not None:
        argv.append(remote)
    return argv


def box_ssh_retry(ctx, ip, user, remote_cmd, *, timeout, attempts, what):
    last_err = ""
    for attempt in range(1, attempts + 1):
        try:
            r = ssh_via_gateway(ctx, ip, remote_cmd, timeout=timeout, user=user)
            if r.returncode == 0:
                return True, ""
            last_err = r.stderr or r.stdout or ""
            if any(s in last_err for s in ("Permission denied", "Host key verification failed")):
                return False, last_err
        except subprocess.TimeoutExpired:
            last_err = f"timeout after {timeout}s"
        if attempt < attempts:
            with PRINT_LOCK:
                print(f"    {what} attempt {attempt}/{attempts} failed for {ip}, retrying in 15s...")
            time.sleep(15)
    return False, last_err


def forget_engine_host_key(ip, known_hosts=None):
# Retire the engine's pinned key and ControlMaster socket — a surviving master keeps speaking
# with the old host key.
    kh = known_hosts or DEFAULT_KNOWN_HOSTS
    if Path(kh).exists():
        subprocess.run(["ssh-keygen", "-R", ip, "-f", kh], capture_output=True, text=True)
    for sock in _master_sockets(kh, ip):
        try:
            subprocess.run(["ssh", "-o", f"ControlPath={sock}", "-O", "exit", f"x@{ip}"],
                           capture_output=True, timeout=5)
        except subprocess.SubprocessError:
            pass
        try:
            sock.unlink()
        except OSError:
            pass


def _master_sockets(kh, ip):
    # The socket is named by ssh's own %C hash, so the old `cm-<ip>-*` glob could never match it;
    # `ssh -G` prints the expanded ControlPath, letting ssh keep ownership of the hash algorithm.
    r = subprocess.run(["ssh", "-G", "-o", f"ControlPath={Path(kh).parent / 'cm-%C'}",
                        f"{config.active().engine.username}@{ip}"],
                       capture_output=True, text=True, timeout=10)
    return [Path(line.split(" ", 1)[1]) for line in (r.stdout or "").splitlines()
            if line.startswith("controlpath ") and Path(line.split(" ", 1)[1]).exists()]


def ssh_via_gateway(ctx, target_ip, cmd, timeout=60, user="ubuntu"):
    args = box_ssh_argv(ctx, target_ip, user, cmd)
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode == 255 and "REMOTE HOST IDENTIFICATION HAS CHANGED" in (r.stderr or ""):
        # A re-cloned engine serves a new host key while the TOFU pin survives, hard-failing
        # every proxied call; re-pin once and retry.
        forget_engine_host_key(ctx["scoring_engine_ip"])
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r


PAM_PREAUTH_SIGNATURE = "by PAM account configuration"


def classify_ssh_failure(ctx, target_ip, user=None, timeout=25):
# 'pam-trap' is the planted-box restart wound: the TCP handshake completes and sshd kills the
# session preauth with the PAM account-stage fatal (distinct from a box still booting).
    args = box_ssh_argv(ctx, target_ip, user or "ubuntu", "true",
                        extra_opts=["-o", "BatchMode=yes"])
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "unreachable"
    except Exception:
        return "unreachable"
    if r.returncode == 0:
        return "ok"
    if PAM_PREAUTH_SIGNATURE in (r.stderr or ""):
        return "pam-trap"
    return "unreachable"


def ssh_on_gateway(ctx, cmd, timeout=30):
    return subprocess.run(
        ["ssh", "-i", ctx["ssh_key_path"], *engine_ssh_opts(ctx),
         f"{ctx['vm_username']}@{ctx['scoring_engine_ip']}", cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def wait_for_ssh(key, user, host, timeout=300):
    print(f"  Waiting for {host} to accept SSH (timeout {timeout}s)...")
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            r = subprocess.run(
                ["ssh", "-i", key, *_engine_opts(host=host),
                 "-o", "BatchMode=yes",
                 f"{user}@{host}", "true"],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0:
                print(f"    {host} reachable via SSH (after {attempt} attempt(s))")
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(5)
    print(f"  WARNING: {host} not reachable via SSH within {timeout}s — continuing anyway")
    record_degradation("box not reachable via SSH", f"{host}: no answer in {timeout}s")
    return False


def wait_for_boxes_ssh(ctx, targets, timeout=300):
    default_node = config.active().proxmox.node
    print("  Waiting for team boxes to accept SSH via gateway...")
    deadline = time.time() + timeout

    def _probe(t):
        ip = t["ip"]
        # Each box may live on its own host in a multi-node range: the guest-agent channel is
        # node-scoped, so ask the box's node, never the engine's.
        node = t.get("node") or default_node
        windows = is_windows_template(t["box"]["template"])
        while time.time() < deadline:
            try:
                if windows:
                    ok = wait_for_guest_agent(node, t["vmid"], timeout=20)
                else:
                    ok = ssh_via_gateway(ctx, ip, "true", timeout=20,
                                         user=ctx.get("box_username", "ubuntu")).returncode == 0
                if ok:
                    with PRINT_LOCK:
                        print(f"    {ip} reachable")
                    return True
            except Exception:
                pass
            time.sleep(10)
        # Diagnose OUTSIDE the print lock: it is a 10 s API/guest poll, and holding the lock
        # across it stalls every other worker's progress line.
        diagnosis = diagnose_unreachable_box(node, t["vmid"])
        with PRINT_LOCK:
            print(f"    WARNING: {ip} not reachable within timeout — continuing")
            print(diagnosis)
            record_degradation("box not reachable within timeout", f"{ip} (vmid {t['vmid']})")
        return False

    results = run_concurrent(targets, _probe)
    if targets and all(r is not True for r in results):
        raise RuntimeError(
            f"All {len(targets)} team box(es) failed to become SSH-reachable — this looks systemic "
            f"(see the guest-agent diagnosis above for each box), not a one-off timing fluke. "
            f"Aborting rather than burning through the DNS/auth/nakon retry loops for boxes "
            f"that are already known unreachable."
        )


def wait_for_cloud_init(ctx, targets, timeout=240):
    print("  Waiting for cloud-init to finish on all team boxes...")
    deadline = time.time() + timeout

    def _wait(t):
        if is_windows_template(t["box"]["template"]):
            return "skipped"
        ip = t["ip"]
        remaining = max(int(deadline - time.time()), 15)
        try:
            r = ssh_via_gateway(ctx, ip, "cloud-init status --wait", timeout=remaining,
                                user=ctx.get("box_username", "ubuntu"))
            if r.returncode in (0, 2):
                with PRINT_LOCK:
                    print(f"    {ip}: cloud-init done (rc={r.returncode})")
                return "ok"
            with PRINT_LOCK:
                print(f"  WARNING: {ip} cloud-init status --wait exited {r.returncode}: "
                      f"{(r.stdout or '').strip()[:150]}")
            return f"rc={r.returncode}"
        except subprocess.TimeoutExpired:
            with PRINT_LOCK:
                print(f"  WARNING: {ip} cloud-init still running after {remaining}s — continuing anyway")
                record_degradation("cloud-init still running", f"{ip}: {remaining}s")
            return "timeout"
        except Exception as e:
            with PRINT_LOCK:
                print(f"  WARNING: cloud-init wait failed for {ip}: {e}")
            return "error"

    run_concurrent(targets, _wait)


def wait_for_http(url, timeout=120):
    print(f"  Waiting for {url} to respond (timeout {timeout}s)...")
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            r = requests.get(url, timeout=5)
            # A 5xx means "up but not serving yet" — the classic Quotient/portal boot race.
            if r.status_code < 500:
                print(f"    {url} responded (after {attempt} attempt(s))")
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(3)
    print(f"  WARNING: {url} did not respond within {timeout}s — continuing anyway")
    record_degradation("endpoint did not respond", f"{url}: {timeout}s")
    return False


def quote_sshkeys(public_key):
# Proxmox's `sshkeys` config param wants the key URL-encoded.
    from urllib.parse import quote
    return quote(public_key.strip(), safe="")
