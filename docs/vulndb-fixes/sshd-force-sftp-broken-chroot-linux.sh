#!/bin/bash
# sshd-force-sftp-broken-chroot (linux) -- fixed body
#
# Finding (deliberately preserved): a `Match Group sftpusers` block that forces
# `internal-sftp` and chroots the member to `/`, i.e. a planted landing zone with
# no shell, no port forwarding and no filesystem view outside the chroot.
#
# Defects fixed:
#  1. The group `sftpusers` was never created. `Match Group` can only ever match a
#     user who is a member of that group, so with the group missing the planted
#     block was INERT -- no user could ever be placed into the finding.
#  2. `systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true`
#     hid every failure, including a genuinely invalid config. Validation is now
#     explicit (`sshd -t`), happens before the reload, and aborts the step.
#  3. Debian/Ubuntu call the unit `ssh`, RHEL/Fedora call it `sshd`; the unit is
#     now detected instead of guessed, and the reload rc is propagated.
#
# Idempotent: phase 5 re-runs over an already-planted box; the Match block is only
# appended when absent, and a pre-existing block (from the old broken body) is
# repaired in place by creating the group.
set -euo pipefail

conf=/etc/ssh/sshd_config
group=sftpusers

if [ ! -f "$conf" ]; then
    echo "sshd-force-sftp-broken-chroot: $conf not found (is openssh-server installed?)" >&2
    exit 1
fi

# ---- 1. the group the Match block keys on must exist -------------------------
group_exists() {
    getent group "$1" >/dev/null 2>&1 || grep -q "^$1:" /etc/group 2>/dev/null
}

if ! group_exists "$group"; then
    if command -v groupadd >/dev/null 2>&1; then
        groupadd "$group"
    elif command -v addgroup >/dev/null 2>&1; then
        addgroup "$group"
    else
        echo "sshd-force-sftp-broken-chroot: cannot create group '$group' (no groupadd/addgroup)" >&2
        exit 1
    fi
fi

# ---- 2. plant the finding, exactly once -------------------------------------
if ! grep -Eq "^[[:space:]]*Match[[:space:]]+Group[[:space:]]+${group}([[:space:]]|\$)" "$conf"; then
    cat >> "$conf" <<EOF

Match Group ${group}
    ForceCommand internal-sftp
    ChrootDirectory /
EOF
fi

# sshd -t refuses to run without its privilege-separation directory (systemd creates
# it at start via RuntimeDirectory=sshd). Create it so validation fails for the
# right reason on a box whose sshd has not started yet.
mkdir -p /run/sshd
chmod 0755 /run/sshd

# ---- 3. validate -- never swallow this --------------------------------------
if ! sshd -t; then
    echo "sshd-force-sftp-broken-chroot: 'sshd -t' rejected the new configuration; not reloading" >&2
    exit 1
fi

# ---- 4. reload the unit that actually exists, and propagate its rc -----------
unit=""
if command -v systemctl >/dev/null 2>&1; then
    # probed with `systemctl show -p LoadState` (a `list-unit-files | grep -q`
    # pipeline SIGPIPEs systemctl under `set -o pipefail`, and `systemctl cat`
    # SIGPIPEs its pager on Fedora and returns 141 -- either would silently skip
    # the reload)
    for u in ssh sshd; do
        if [ "$(systemctl show -p LoadState --value "$u.service" 2>/dev/null)" = loaded ]; then
            unit="$u"
            break
        fi
    done
fi

if [ -n "$unit" ]; then
    if systemctl is-active --quiet "$unit"; then
        systemctl reload "$unit"
    fi
    # Inactive unit (e.g. socket-activated ssh.socket on Ubuntu 24.04) has no
    # running config to refresh; the next start reads the validated file.
elif command -v rc-service >/dev/null 2>&1 && rc-service sshd status >/dev/null 2>&1; then
    rc-service sshd reload
fi
