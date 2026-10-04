"""Transports: how one wanted item gets from its source into the test folder."""

import os
import shutil
import subprocess
from pathlib import Path

from config_ops import write_text_atomic

from .constants import Unreachable
from .hashing import seal_local_file, write_bytes_atomic


def default_transport():
    """route name -> fetch callable. Injected so the collector is testable with no estate."""
    return {"scp-jump": _scp_files, "guest-agent": _guest_files, "local": _local_files,
            "ssh-cmd": _ssh_capture}

def _ssh_options(ssh):
    """(user, host, common option args, jump option) shared by the scp and ssh channels."""
    key, user, host = ssh.get("key"), ssh.get("user") or "sysadmin", ssh.get("host")
    if not host:
        raise Unreachable("no address recorded for this target")
    common = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
              "-o", "ConnectTimeout=15"]
    if key:
        common += ["-i", str(key)]
    return user, host, common, ssh.get("jump")


def _scp_files(ssh, remote, dest_dir, timeout=120):
    """scp `remote` into dest_dir. Globs are expanded by the remote shell, so a
    `/var/lib/bad-auto/report-*.md` pattern needs no directory listing round-trip.

    Direct first, then through the engine jump: the control host reaches red01 either way
    depending on the estate, and the harness has used this two-attempt shape since the first
    scrim (run-agent-scrim.py:1906-1914). A failed attempt's partial file is always removed —
    a truncated events.jsonl that looks complete is worse than a loud failure."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    user, host, common, jump = _ssh_options(ssh)
    before = {p.name for p in dest_dir.iterdir() if p.is_file()}
    last_error = ""
    for extra in ([], (["-o", jump] if jump else [])):
        cmd = ["scp"] + common + extra + [f"{user}@{host}:{remote}", f"{dest_dir}/"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last_error = f"scp timed out after {timeout}s"
            continue
        fetched = [p for p in dest_dir.iterdir() if p.is_file() and p.name not in before]
        lowered = (proc.stderr or proc.stdout or "").lower()
        if proc.returncode == 0 and fetched:
            for path in fetched:
                os.chmod(path, 0o600)
            return sorted(fetched)
        for path in fetched:
            path.unlink(missing_ok=True)
        last_error = (proc.stderr or proc.stdout or "").strip()
        if "no such file" in lowered or "not found" in lowered:
            raise FileNotFoundError(f"{remote} on {host}")
    raise Unreachable(f"scp from {user}@{host} failed: {last_error}")


def _guest_files(source, remote, dest_dir, timeout=60):
    """Read one file from a team box over the guest-agent channel (Windows branch included).

    The agent channel is the only route that needs no network path (virtio-serial), which is why
    it is preferred for team boxes; `range_ops.guest_file_read` raises FileNotFoundError vs
    RuntimeError precisely so this collector can record `absent` vs `failed`."""
    if any(ch in remote for ch in "*?["):
        raise RuntimeError(
            f"the guest-agent route cannot expand {remote!r} — list the files with a shell exec "
            "first, or use the scp route for this target")
    import range_ops  # lazy: keeps this module importable without the Proxmox client stack

    dest = Path(dest_dir) / Path(remote).name
    data = range_ops.guest_file_read(source["node"], int(source["vmid"]), remote,
                                     timeout=timeout, windows=bool(source.get("windows")))
    return [write_bytes_atomic(dest, data)]


def _local_files(source, pattern, dest_dir):
    """Copy local files into the test folder. `pattern` is relative to the target root."""
    root, dest_dir = Path(source["root"]), Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    matches = sorted(root.glob(pattern))
    if not matches:
        raise FileNotFoundError(str(root / pattern))
    written = []
    for src in matches:
        dest = dest_dir / src.name
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
            for path in dest.rglob("*"):
                if path.is_file():
                    os.chmod(path, 0o600)
                    written.append(path)
            continue
        seal_local_file(src, dest)
        written.append(dest)
    return written


def _dest_dir(test_path, target, want):
    """Where this wanted item's bytes go, as a directory."""
    if want.get("local"):
        return (Path(test_path) / want["local"]).parent
    return Path(test_path) / (want.get("to") or target.get("to")
                              or f"evidence/{target['name'].split(':')[0]}/")


def _ssh_capture(ssh, cmd, dest_file, timeout=90):
    """Run a read-only command over ssh and save its stdout (red's journal, mainly).

    Same direct-then-engine-jump shape as the file pull. There is no `scp` equivalent for a
    service journal, and the journal is the only record of *why* the red LLM stopped mid-event,
    so it is worth one command channel."""
    dest_file = Path(dest_file)
    user, host, common, jump = _ssh_options(ssh)
    last_error = ""
    for extra in ([], (["-o", jump] if jump else [])):
        try:
            proc = subprocess.run(["ssh"] + common + extra + [f"{user}@{host}", cmd],
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last_error = f"ssh timed out after {timeout}s"
            continue
        if proc.returncode == 0:
            if not (proc.stdout or "").strip():
                raise FileNotFoundError(f"no output from {cmd!r}")
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(dest_file, proc.stdout, mode=0o600)
            return [dest_file]
        last_error = (proc.stderr or proc.stdout or "").strip()
    raise Unreachable(f"ssh to {user}@{host} failed: {last_error}")


def fetch(target, want, test_path, timeout, transport):
    """Fetch one wanted item. Returns the list of local paths written."""
    if want.get("cmd"):
        fetcher = transport.get("ssh-cmd")
        if fetcher is None:
            raise RuntimeError("no transport for the ssh command channel")
        dest = Path(test_path) / (want.get("local") or f"evidence/{target['name']}/cmd.log")
        return fetcher(target.get("ssh") or {}, want["cmd"], dest, timeout=timeout)
    fetcher = transport.get(target.get("route"))
    if fetcher is None:
        raise RuntimeError(f"no transport for route {target.get('route')!r}")
    dest_dir = _dest_dir(test_path, target, want)
    if target.get("route") == "local":
        source = {"name": target["name"], "root": target["root"]}
        return fetcher(source, want.get("local_name") or want["pattern"], dest_dir)
    if target.get("route") == "scp-jump":
        return fetcher(target.get("ssh") or {}, want.get("remote") or want.get("pattern"),
                       dest_dir, timeout=timeout)
    if target.get("route") == "guest-agent":
        return fetcher(target, want.get("remote") or want.get("pattern"), dest_dir,
                       timeout=timeout)
    raise RuntimeError(f"unknown route {target.get('route')!r}")
