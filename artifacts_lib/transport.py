"""Transports: how one wanted item gets from its source into the test folder."""

import os
import shutil
import subprocess
import time
from pathlib import Path

from config.config_ops import write_text_atomic

from .constants import Unreachable
from .hashing import seal_local_file


def default_transport():
    return {"scp-jump": _scp_files, "local": _local_files, "ssh-cmd": _ssh_capture}

def _ssh_options(ssh):
    key, user, host = ssh.get("key"), ssh.get("user") or "sysadmin", ssh.get("host")
    if not host:
        raise Unreachable("no address recorded for this target")
    common = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
              "-o", "ConnectTimeout=15"]
    if key:
        common += ["-i", str(key)]
    return user, host, common, ssh.get("jump")


def _scp_files(ssh, remote, dest_dir, timeout=120):
    # Globs expand remote-side; a failed attempt removes its partial file — a truncated
    # events.jsonl that looks complete is worse than a loud failure. The route pair
    # retries once after 10s: a teardown lost the red report to a seconds-long
    # jump-address flap. "No such file" is a real absence — it raises immediately.
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    user, host, common, jump = _ssh_options(ssh)
    before = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
              for p in dest_dir.iterdir() if p.is_file()}
    last_error = ""
    routes = ([], (["-o", jump] if jump else []))
    for round_no in (1, 2):
        for extra in routes:
            cmd = ["scp"] + common + extra + [f"{user}@{host}:{remote}", f"{dest_dir}/"]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                last_error = f"scp timed out after {timeout}s"
                continue
            changed = [p for p in dest_dir.iterdir() if p.is_file()
                       and before.get(p.name) != (p.stat().st_mtime_ns, p.stat().st_size)]
            lowered = (proc.stderr or proc.stdout or "").lower()
            if proc.returncode == 0:
                # rc=0 is the truth. A destination that already existed (the harness
                # pulled it before teardown) is overwritten in place, so "no NEW names"
                # is not a failure — that reading recorded a present report as unreachable.
                if not changed and not any(ch in remote for ch in "*?["):
                    cand = dest_dir / Path(remote).name
                    if cand.is_file():
                        changed = [cand]
                if changed:
                    for path in changed:
                        os.chmod(path, 0o600)
                    return sorted(changed)
                raise FileNotFoundError(f"{remote} on {host}")
            for path in changed:
                path.unlink(missing_ok=True)
            last_error = (proc.stderr or proc.stdout or "").strip()
            if "no such file" in lowered or "not found" in lowered:
                raise FileNotFoundError(f"{remote} on {host}")
        if round_no == 1:
            time.sleep(10)
    raise Unreachable(f"scp from {user}@{host} failed: {last_error}")


def _local_files(source, pattern, dest_dir):
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


def _ssh_capture(ssh, cmd, dest_file, timeout=90):
    # Read-only commands only. No scp equivalent exists for a service journal, and the
    # journal is the only record of why the red LLM stopped mid-event.
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
    if want.get("cmd"):
        fetcher = transport.get("ssh-cmd")
        if fetcher is None:
            raise RuntimeError("no transport for the ssh command channel")
        dest = Path(test_path) / (want.get("local") or f"evidence/{target['name']}/cmd.log")
        return fetcher(target.get("ssh") or {}, want["cmd"], dest, timeout=timeout)
    fetcher = transport.get(target.get("route"))
    if fetcher is None:
        raise RuntimeError(f"no transport for route {target.get('route')!r}")
    if want.get("local"):
        dest_dir = (Path(test_path) / want["local"]).parent
    else:
        dest_dir = Path(test_path) / (want.get("to") or target.get("to")
                                      or f"evidence/{target['name'].split(':')[0]}/")
    if target.get("route") == "local":
        source = {"name": target["name"], "root": target["root"]}
        return fetcher(source, want.get("local_name") or want["pattern"], dest_dir)
    if target.get("route") == "scp-jump":
        return fetcher(target.get("ssh") or {}, want.get("remote") or want.get("pattern"),
                       dest_dir, timeout=timeout)
    raise RuntimeError(f"unknown route {target.get('route')!r}")
