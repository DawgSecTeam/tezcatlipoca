"""Guest-agent channels: exec as root/SYSTEM, detached exec, file pull, agent waits."""

import base64
import shlex
import time
from typing import NamedTuple

from pve_api import proxmox_api

def diagnose_unreachable_box(node, vmid):
    """Diagnose unreachable box via guest agent (virtio-serial, no network needed). Never raises."""

    try:
        ifaces = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
        )["data"]["result"]
        addrs = [
            a["ip-address"] for iface in ifaces for a in iface.get("ip-addresses", [])
            if a.get("ip-address-type") == "ipv4" and not a["ip-address"].startswith("127.")
        ]
        iface_summary = f"has IPv4 {', '.join(addrs)}" if addrs else "has NO IPv4 address on any interface"
    except Exception as e:
        return f"      (guest agent unreachable for vmid {vmid}, can't diagnose further: {e})"

    cloud_init_summary = "(cloud-init status unavailable)"
    try:
        pid = proxmox_api(
            "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
            data={"command": ["cloud-init", "status", "--long"]},
        )["data"]["pid"]
        deadline = time.time() + 10
        while time.time() < deadline:
            s = proxmox_api(
                "GET", f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
                params={"pid": pid},
            )["data"]
            if s.get("exited"):
                out = (s.get("out-data") or "").strip()
                cloud_init_summary = out.splitlines()[0] if out else "(no output)"
                break
            time.sleep(0.5)
    except Exception:
        pass

    return f"      guest agent (vmid {vmid}): {iface_summary}; cloud-init {cloud_init_summary}"


def guest_agent_exec_root(node, vmid, script, timeout=60, shell="bash"):
    """Run a shell script as root via the QEMU guest agent; returns (exit_code, stdout,
    stderr). shell="sh" for guests without bash (the alpine jump VM)."""
    pid = proxmox_api(
        "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
        data={"command": [shell, "-c", script]},
    )["data"]["pid"]
    last = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
            params={"pid": pid},
        )["data"]
        if s.get("exited"):
            return s.get("exitcode", -1), s.get("out-data", ""), s.get("err-data", "")
        last = s
        time.sleep(1)
    raise RuntimeError(_exec_timeout_message(vmid, pid, timeout, last))


def guest_agent_exec_windows(node, vmid, ps_script, timeout=120):
    """Run PowerShell as SYSTEM via the guest agent; only channel before Windows bootstrap."""
    import base64
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    pid = proxmox_api(
        "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
        data={"command": [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
        ]},
    )["data"]["pid"]
    last = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
            params={"pid": pid},
        )["data"]
        if s.get("exited"):
            return s.get("exitcode", -1), s.get("out-data", ""), s.get("err-data", "")
        last = s
        time.sleep(1)
    raise RuntimeError(_exec_timeout_message(vmid, pid, timeout, last))


# The guest-file pull block. It lives next to the two exec helpers above because
# those are its fallback channels: agent/file-read is only usable where PVE and the
# guest agent both implement it, and everywhere else the bytes come back base64'd
# through an exec — which is why the read side has a practical size ceiling too
# (the same endpoint's write side caps at 61440 base64 chars, pmx.py:21-26).
_NOT_FOUND_SIGNATURES = (
    "no such file", "not found", "does not exist",
    # Windows: [IO.File]::ReadAllBytes fails with .NET's "Could not find file ...",
    # which contains neither "no such file" nor "not found".
    "could not find", "cannot find",
)


def _guest_file_missing(detail):
    """Is this error string "the file is not there", or "the channel broke"?

    One predicate for all three channels, because downstream the two outcomes are
    recorded differently (`absent` vs `failed` in a collection manifest): an absent
    file is a normal result — a run that never wrote that log — while a read that
    fails needs an operator. PVE answers a missing file with an HTTP error and the
    guest agent with rc != 0, so the message text is the only portable signal. The
    Windows phrasings are here because .NET's "Could not find file" matches neither
    "no such file" nor "not found"; without them an absent Windows path would be
    misfiled as a transport failure and re-probed through every channel.
    """
    low = str(detail or "").lower()
    return any(sig in low for sig in _NOT_FOUND_SIGNATURES)


def _guest_file_content_bytes(content):
    """Bytes for the `content` field of a PVE agent/file-read response.

    The channel is asymmetric: file-write wants base64 that the caller produces
    (pmx.py:21-26), file-read documents base64 back — yet the proxmoxer path the
    skill CLI uses has returned `content` already decoded (pmx.py:25,591). So decide
    per response rather than per Proxmox version: a body that is valid base64 (right
    length, padding, no whitespace) is decoded; anything else — a space, a trailing
    newline, the wrong length — is literal text, which is what an already-decoded
    body looks like. A short text file consisting only of base64 characters is
    genuinely indistinguishable from an encoded one, which is one more reason the scp
    route stays the byte-exact one for arbitrary evidence.
    """
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    text = str(content)
    probe = text.strip()
    if probe and len(probe) % 4 == 0 and not any(c.isspace() for c in probe):
        try:
            return base64.b64decode(probe, validate=True)
        except (ValueError, TypeError):
            pass
    return text.encode("utf-8", errors="surrogateescape")


def guest_file_read(node, vmid, path, timeout=60, windows=False, max_bytes=8388608):
    """Pull one guest file's exact bytes — this repo's only file-pull primitive.

    Why it exists: every other agent helper here runs a command and hands back
    `(exit_code, stdout, stderr)` (`guest_agent_exec_root` :214,
    `guest_agent_exec_windows` :235), so an artifact could only be rebuilt by parsing
    stdout, and the one proven pull in the tree (red01's evidence,
    run-agent-scrim.py) is scp over an engine jump. Evidence collection needs what a
    manifest can trust — bytes, or a definite "not there" — and the absent-vs-failed
    split is the whole point: a missing file raises `FileNotFoundError` (record
    `absent`; a run that never wrote its log is normal) and a broken channel raises
    `RuntimeError` (record `failed`; a human is needed). Never collapse the two.

    Channels, in this order:

    1. PVE `GET /nodes/<node>/qemu/<vmid>/agent/file-read` — no guest-side tooling
       and no shell, identical on Linux and Windows, so it is tried first.
    2. Linux fallback: `base64 <file> | tr -d '\\n'` through
       `guest_agent_exec_root`, for PVE/agent versions without file-read. The form
       matters: `-w0` is GNU-only, while `base64 ... | tr -d '\\n'` is the one GNU
       coreutils and busybox agree on; the pipeline is POSIX, so it runs under `sh`
       (alpine guests have no bash).
    3. `windows=True` fallback:
       `[Convert]::ToBase64String([IO.File]::ReadAllBytes('<path>'))` through
       `guest_agent_exec_windows`.

    A not-found signature in the primary route's error is final — PVE already
    answered, so the exec channel is not spent re-asking. Any *other* primary failure
    falls through to the exec channel, and only if that fails too does the error
    surface, as a `RuntimeError` naming both causes.

    `max_bytes` bounds the decoded result, not the transfer: a silently truncated
    evidence file that looks complete is worse than a loud failure, so an oversize
    read raises. Use this for config files, unit status, small logs and journals you
    need byte-exact; keep multi-MB evidence (e.g. `events.jsonl`) on the existing scp
    route, because file-read ships the whole file base64'd in one JSON body and has
    no chunk or offset parameter.

    `timeout` is per channel and bounds the exec poll loops, not the HTTP read.
    """
    data = None
    primary_error = None
    try:
        resp = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/file-read",
            params={"file": path},
        )
        content = (resp.get("data") or {}).get("content")
        if content is None:
            raise RuntimeError("agent/file-read returned no content field")
        data = _guest_file_content_bytes(content)
    except Exception as e:
        if _guest_file_missing(e):
            # PVE spells a missing file as an HTTP error; that IS the answer, so do
            # not spend the fallback (and another timeout) re-asking the same question.
            raise FileNotFoundError(
                f"guest file {path} does not exist in vmid {vmid} (node {node}): {e}"
            ) from e
        primary_error = e

    if data is None:
        if windows:
            channel = "powershell"
            # Single-quoted PowerShell literal: the only escape is a doubled quote.
            script = ("[Convert]::ToBase64String([IO.File]::ReadAllBytes('"
                      + path.replace("'", "''") + "'))")
        else:
            channel = "base64"
            script = f"base64 {shlex.quote(path)} | tr -d '\\n'"
        try:
            if windows:
                rc, out, err = guest_agent_exec_windows(node, vmid, script, timeout=timeout)
            else:
                rc, out, err = guest_agent_exec_root(node, vmid, script,
                                                     timeout=timeout, shell="sh")
        except Exception as e:
            raise RuntimeError(
                f"guest file read of {path} on vmid {vmid} failed on both channels — "
                f"agent/file-read: {primary_error}; {channel} exec fallback: {e}"
            ) from e
        if rc != 0:
            if _guest_file_missing(err):
                raise FileNotFoundError(
                    f"guest file {path} does not exist in vmid {vmid} (node {node}): "
                    f"rc={rc} from the {channel} exec fallback: {str(err).strip()}")
            raise RuntimeError(
                f"guest file read of {path} on vmid {vmid} failed on the {channel} exec "
                f"fallback (rc={rc}): {str(err).strip()}; agent/file-read had failed "
                f"first: {primary_error}")
        try:
            # validate=True: a corrupted stream must raise here, not silently decode to
            # fewer bytes (without it b64decode discards junk characters).
            data = base64.b64decode((out or "").strip(), validate=True)
        except (ValueError, TypeError) as e:
            raise RuntimeError(
                f"guest file read of {path} on vmid {vmid}: the {channel} exec fallback "
                f"returned {len(out or '')} chars that are not valid base64: {e}") from e

    if len(data) > max_bytes:
        raise RuntimeError(
            f"guest file read of {path} on vmid {vmid} returned {len(data)} bytes, over the "
            f"max_bytes limit of {max_bytes} — refusing to hand back a file that only looks "
            f"complete. Pull large evidence (multi-MB events.jsonl) over the scp route.")
    return data


def _exec_timeout_message(vmid, pid, timeout, status):
    """Explain a guest-exec timeout instead of just naming it.

    A bare "didn't finish within Ns" gives an operator nothing to act on, and five
    exec logs carry exactly that at the 120s cap (live-found 2026-09-26..10-01). The
    agent already holds the partial output by the time we give up — include it, and
    point at the detached path for work that legitimately outlives a poll budget.
    """
    status = status or {}
    detail = []
    for label, key in (("stdout", "out-data"), ("stderr", "err-data")):
        text = str(status.get(key) or "").strip()
        if text:
            detail.append(f"{label} so far: {text[-400:]!r}")
    tail = ("; " + "; ".join(detail)) if detail else ""
    return (f"guest-agent exec pid={pid} on vmid {vmid} did not finish within {timeout}s"
            f"{tail}. For work that can outlive the budget, call "
            f"guest_agent_exec_detached() (nohup + log polling) rather than raising "
            f"the timeout.")


class DetachedExecResult(NamedTuple):
    """`rc` is the script's exit status; `log` is the tail of the guest-side log;
    `log_path` is where that log lives ON THE GUEST (not stderr — a detached run
    has one merged stream, and the file outlives the call)."""

    rc: int
    log: str
    log_path: str


# Written by the same shell that ran the payload, so it cannot appear in the log
# before the payload has finished.
DETACHED_RC_MARKER = "__TZ_DETACHED_RC="


def guest_agent_exec_detached(node, vmid, script, log_path, timeout=1800,
                              shell="bash", poll_interval=5):
    """Run a long root script via the guest agent without holding the exec channel open.

    The agent's exec channel is a poll loop with a caller-set budget, so work that
    legitimately outlives that budget (apt installs, nakon bundles, service fixups)
    used to die mid-plant with a timeout — and the plant's partial effects stayed on
    the box. This starts the payload under setsid+nohup, redirects it to a log on the
    guest, and polls that log for a completion marker, so `timeout` is a real deadline
    rather than a request/response budget.

    Returns DetachedExecResult(rc, log_tail, log_path). The log survives a dropped
    agent channel, so a timeout here is diagnosable after the fact on the guest.
    """
    qlog = shlex.quote(log_path)
    # A subshell keeps an `exit` inside the payload from skipping the rc marker, and
    # the marker is written by the same shell that ran the payload — so it cannot
    # appear in the log before the payload has finished.
    body = (f"( {script}\n); __tz_rc=$?; "
            f'printf "%s%s\\n" "{DETACHED_RC_MARKER}" "$__tz_rc" >> {qlog}')
    wrapper = (f"rm -f {qlog}; "
               f"setsid nohup {shell} -c {shlex.quote(body)} "
               f"> {qlog} 2>&1 < /dev/null & echo started")
    proxmox_api(
        "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
        data={"command": [shell, "-c", wrapper]},
    )

    deadline = time.time() + timeout
    last_log = ""
    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            _rc, out, _err = guest_agent_exec_root(
                node, vmid, f"tail -c 4000 {qlog} 2>/dev/null || true", timeout=30)
        except Exception:
            # A transient agent hiccup is expected while the payload churns; the log
            # on the guest is the durable record, so keep polling until the deadline.
            continue
        last_log = out or ""
        marker_at = last_log.rfind(DETACHED_RC_MARKER)
        if marker_at != -1:
            tail = last_log[marker_at + len(DETACHED_RC_MARKER):].strip()
            rc_text = tail.splitlines()[0].strip() if tail else ""
            try:
                rc = int(rc_text)
            except ValueError:
                rc = -1
            return DetachedExecResult(rc, last_log, log_path)
    raise RuntimeError(
        f"guest-agent detached exec on vmid {vmid} did not finish within {timeout}s "
        f"(log on the guest: {log_path}). Last log tail: {last_log[-400:]!r}")


def wait_for_guest_agent(node, vmid, timeout=300):
    """Wait for guest agent ping (virtio-serial, no network needed). Never raises."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/agent/ping", data={})
            return True
        except Exception:
            pass
        time.sleep(5)
    # Never raises — callers depend on the boolean — but a silent False is how a
    # 45-minute escalation looks like progress (observed 600 -> 1200 -> 2700 -> 2900s
    # for one vmid). Say what the VM is actually doing instead.
    print(f"    guest agent on vmid {vmid} did not answer within {timeout}s{_vm_state_suffix(node, vmid)}")
    return False


def _vm_state_suffix(node, vmid):
    """Best-effort ' (status=running, name=…)' for a failed agent wait. Never raises."""
    try:
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
        status = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/status/current")["data"]
        return (f" (status={status.get('status', '?')}, name={cfg.get('name', '?')}, "
                f"lock={cfg.get('lock') or 'none'})")
    except Exception as e:
        return f" (state unavailable: {e})"
