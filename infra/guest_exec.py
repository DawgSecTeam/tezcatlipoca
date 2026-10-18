"""Guest-agent channels: exec as root/SYSTEM, detached exec, file transfer, agent waits."""

import shlex
import time
from typing import NamedTuple

from infra.client import proxmox_api

def diagnose_unreachable_box(node, vmid):
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


def _exec_status(node, vmid, pid):
    # A guest-agent restart (it upgrades itself during first boot) drops every live exec pid;
    # the status call then 500s with "PID … does not exist". That is a lost channel, not the
    # command's exit status — report it as such instead of an HTTPError traceback.
    try:
        return proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
                           params={"pid": pid})["data"]
    except Exception as e:
        if "does not exist" in str(e):
            raise RuntimeError(
                f"guest agent on vmid {vmid} lost exec pid {pid} (the agent restarted "
                f"mid-command) — the command's outcome is unknown") from e
        raise


def guest_agent_exec_root(node, vmid, script, timeout=60, shell="bash"):
    pid = proxmox_api(
        "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
        data={"command": [shell, "-c", script]},
    )["data"]["pid"]
    last = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _exec_status(node, vmid, pid)
        if s.get("exited"):
            return s.get("exitcode", -1), s.get("out-data", ""), s.get("err-data", "")
        last = s
        time.sleep(1)
    raise RuntimeError(_exec_timeout_message(vmid, pid, timeout, last))


def guest_agent_write_file(node, vmid, path, content):
    """Write a local file's bytes to a path in the guest, chunked past the API's content cap."""
    if isinstance(content, (bytes, bytearray, memoryview)):
        data = bytes(content)
    else:
        with open(content, "rb") as f:
            data = f.read()
    parts = []
    for start in range(0, len(data), _FILE_WRITE_CHUNK):
        part = f"{path}.part{len(parts)}"
        _guest_agent_write_chunk(
            node, vmid, part, data[start:start + _FILE_WRITE_CHUNK])
        parts.append(part)
    # file-write is one-shot (no append/offset), so the parts are concatenated guest-side.
    rc, _out, err = guest_agent_exec_root(
        node, vmid,
        f"cat {' '.join(parts)} > {path} && rm -f {' '.join(parts)}", timeout=300)
    if rc != 0:
        raise RuntimeError(f"could not reassemble {path} in vmid {vmid} from "
                           f"{len(parts)} chunks: rc={rc} {err.strip()[:200]}")


def guest_agent_read_file(node, vmid, path, local_path=None):
    """Read a guest file to bytes, or write it to a local path when one is given."""
    # content is a str, so surrogateescape carries non-UTF-8 bytes back out losslessly.
    data = proxmox_api(
        "GET", f"/nodes/{node}/qemu/{vmid}/agent/file-read", params={"file": path},
    )["data"]["content"].encode("utf-8", "surrogateescape")
    if local_path:
        with open(local_path, "wb") as f:
            f.write(data)
    else:
        return data


# file-write caps `content` at 61440 chars and we hand it base64, so the raw chunk budget is what
# base64 expands back up to that cap.
_FILE_WRITE_CHUNK = 61440 // 4 * 3


def _guest_agent_write_chunk(node, vmid, path, chunk):
    import base64
    proxmox_api(
        "POST", f"/nodes/{node}/qemu/{vmid}/agent/file-write",
        data={"file": path, "content": base64.b64encode(chunk).decode("ascii"), "encode": 0},
    )


# The only agent channel before Windows bootstrap brings up the SSH path.
def guest_agent_exec_windows(node, vmid, ps_script, timeout=120):
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
        s = _exec_status(node, vmid, pid)
        if s.get("exited"):
            return s.get("exitcode", -1), s.get("out-data", ""), s.get("err-data", "")
        last = s
        time.sleep(1)
    raise RuntimeError(_exec_timeout_message(vmid, pid, timeout, last))


# Include the agent's partial output: a bare "didn't finish within Ns" gives an operator
# nothing to act on, and point at the detached path for work that outlives a poll budget.
def _exec_timeout_message(vmid, pid, timeout, status):
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
    """`rc` is the script's exit status; `log_path` lives ON THE GUEST and outlives a dropped
    agent channel (a detached run has one merged stream, not stderr)."""

    rc: int
    log: str
    log_path: str


# Written by the same shell that ran the payload, so it can never appear in the log first.
DETACHED_RC_MARKER = "__TZ_DETACHED_RC="


def guest_agent_exec_detached(node, vmid, script, log_path, timeout=1800,
                              shell="bash", poll_interval=5):
    """Run a long root script via the guest agent without holding the exec channel open: the
    payload runs under setsid+nohup to a guest log, polled for a completion marker, so `timeout`
    is a real deadline. Returns DetachedExecResult(rc, log_tail, log_path)."""
    qlog = shlex.quote(log_path)
    # A subshell keeps an `exit` inside the payload from skipping the rc marker.
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
    # Never raises — callers depend on the boolean — but a silent False reads as progress
    # and hides a 45-minute escalation. Say what the VM is doing instead.
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
