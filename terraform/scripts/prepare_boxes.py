#!/usr/bin/env python3
"""
Gets every team box into a state where nakon's deploy.py can actually install things:
repairs DNS, refreshes the package cache, and proves both worked before handing over.

Runs on the scoring engine (the only host with a NIC on every team subnet), invoked from
main.tf's Step D immediately before deploy.py.

This has to run *before* deploy.py, not after. Boxes boot with an empty /etc/resolv.conf
(see DNS_FIX_CMD in utils.py), and nakon installs every service with `apt-get install`,
which has to resolve a mirror first. nakon reports none of this back — install_package()
ignores exit status and deploy.py never raises — so a box that skips this step installs
nothing, Terraform still exits 0, and the whole range comes up with every service down.
Hence the hard failure below: a box that can't resolve is worth stopping the apply for.
"""

import json
import subprocess
import sys

import paramiko

from utils import DNS_FIX_CMD

# Pure DNS reachability test — the name just has to resolve, so this is valid on a box of
# any distro, not only the Debian ones that would install from it.
RESOLVE_PROBE = "getent hosts deb.debian.org"

# Mirrors the package-manager detection in nakon's configurations.py:install_package(), so a
# box gets its cache refreshed by whichever manager nakon will go on to install with.
CACHE_REFRESH_CMD = (
    "if command -v apt-get > /dev/null; then sudo apt-get update; "
    "elif command -v dnf > /dev/null; then sudo dnf makecache; "
    "elif command -v yum > /dev/null; then sudo yum makecache; fi"
)


def run(client, command, timeout=300):
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    # Read to EOF before asking for the exit status: read() blocks until the command finishes,
    # whereas exec_command returns immediately. Closing the client without draining the channel
    # kills the command mid-run — which is why the old inline pre-clean never reliably cleaned.
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    return stdout.channel.recv_exit_status(), out, err


def prepare(machine):
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    scan = subprocess.run(
        ["ssh-keyscan", "-T", "5", "-H", machine["ip"]],
        capture_output=True,
        text=True,
        check=True,
    )
    host_keys = client.get_host_keys()
    for line in scan.stdout.splitlines():
        if not line or line.startswith("#"):
            continue
        entry = paramiko.hostkeys.HostKeyEntry.from_line(line)
        if entry:
            for host in entry.hostnames:
                host_keys.add(host, entry.key.get_name(), entry.key)
    client.connect(
        machine["ip"], username=machine["user"], password=machine["password"], timeout=30
    )
    try:
        # deploy.py stages attachments in /tmp and never cleans up after itself. /tmp is a
        # RAM-backed tmpfs at half the box's memory, so leftovers from an earlier failed run
        # can fill it and break both apt and the next SFTP transfer.
        run(client, "find /tmp -maxdepth 1 -type f -delete")

        run(client, DNS_FIX_CMD)
        status, _out, _err = run(client, RESOLVE_PROBE)
        if status != 0:
            raise RuntimeError("cannot resolve DNS even after the resolv.conf fix")

        status, _out, err = run(client, CACHE_REFRESH_CMD)
        if status != 0:
            raise RuntimeError(f"package cache refresh failed (exit {status}): {err.strip()}")
    finally:
        client.close()


def main():
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} <path to nakon config.json>")

    with open(sys.argv[1]) as f:
        machines = json.load(f)["machines"]

    failures = []
    for machine in machines:
        try:
            prepare(machine)
            print(f"[prep] {machine['ip']}: DNS resolves, package cache refreshed")
        except Exception as e:
            failures.append(machine["ip"])
            print(f"[prep] {machine['ip']}: FAILED — {e}", file=sys.stderr)

    if failures:
        print(
            f"\n[prep] {len(failures)}/{len(machines)} box(es) not ready: {', '.join(failures)}.\n"
            "[prep] nakon would install nothing on them and still report success, leaving every\n"
            "[prep] service down on the scoreboard — stopping here instead.",
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
