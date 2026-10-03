#!/bin/bash
# tftpd-hpa-anon-write (linux) -- fixed body
#
# Finding (deliberately preserved): a world-writable TFTP root served with
# --create/--permissive, i.e. unauthenticated anonymous READ **and** WRITE of
# arbitrary files over UDP/69.
#
# Defects fixed:
#  1. INERT PLANT (the real Noble defect). The package postinst starts the daemon
#     with the distro defaults (`in.tftpd --secure /srv/tftp` on Ubuntu). The old
#     body then edited /etc/default/tftpd-hpa and ran
#     `systemctl enable --now tftpd-hpa` -- a no-op for a unit that is already
#     active -- so the new options were never loaded and uploads were refused
#     (curl rc=68). The unit is now restarted after the config change.
#  2. The old body only sed-rewrote keys that already existed; keys are now set
#     whether or not the shipped file carries them.
#  3. RHEL/Fedora ship tftp.socket + tftp.service (`in.tftpd -s /var/lib/tftpboot`)
#     and ignore /etc/default/tftpd-hpa entirely; the create/permissive flags are
#     now injected through a systemd drop-in on that path.
#  4. The install could leave dpkg wedged and silently break every later config
#     step; dpkg is now asserted clean and the step fails loudly if it is not.
#  5. The final `|| true` service start is replaced by a real assertion: the
#     script performs an unauthenticated upload+download round trip and fails if
#     the finding is not actually live.
#
# Idempotent: keys are replaced in place, the drop-in is rewritten, and the
# service is restarted on every run.
set -euo pipefail

root=/var/lib/tftpboot
short_opts='-s -c -p'
long_opts='--secure --create --permissive'

# ---- 1. install the server --------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y tftpd-hpa
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y tftp-server
elif command -v yum >/dev/null 2>&1; then
    yum install -y tftp-server
elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache tftp-hpa
else
    echo "tftpd-hpa-anon-write: no supported package manager (apt-get/dnf/yum/apk)" >&2
    exit 1
fi

# A half-configured package must never cascade into every later apt/dpkg step.
if command -v dpkg >/dev/null 2>&1; then
    dpkg --configure -a >/dev/null 2>&1 || true
    if [ -n "$(dpkg --audit 2>/dev/null || true)" ]; then
        echo "tftpd-hpa-anon-write: dpkg is NOT clean after installing tftpd-hpa:" >&2
        dpkg --audit >&2 || true
        exit 1
    fi
fi

# ---- 2. plant the finding ---------------------------------------------------
mkdir -p "$root"
chmod 0777 "$root"

set_key() { # file key value
    touch "$1"
    if grep -Eq "^[[:space:]]*$2=" "$1"; then
        sed -i "s|^[[:space:]]*$2=.*|$2=$3|" "$1"
    else
        printf '%s=%s\n' "$2" "$3" >>"$1"
    fi
}

# Debian/Ubuntu: the sysv-generated tftpd-hpa unit reads these.
set_key /etc/default/tftpd-hpa TFTP_DIRECTORY "\"$root\""
set_key /etc/default/tftpd-hpa TFTP_OPTIONS "\"$long_opts\""

# RHEL/Fedora/Alma: socket-activated tftp.service ignores /etc/default.
if command -v systemctl >/dev/null 2>&1 &&
    [ "$(systemctl show -p LoadState --value tftp.service 2>/dev/null)" = loaded ]; then
    dropdir=/etc/systemd/system/tftp.service.d
    mkdir -p "$dropdir"
    cat >"$dropdir/10-anon-write.conf" <<EOF
[Service]
ExecStart=
ExecStart=/usr/sbin/in.tftpd $short_opts $root
EOF
fi

# Legacy inetd/xinetd path (older RHEL/CentOS).
if [ -f /etc/xinetd.d/tftp ]; then
    sed -i "s|^\([[:space:]]*server_args[[:space:]]*=\).*|\1 $short_opts $root|" /etc/xinetd.d/tftp
fi

# ---- 3. (re)start the unit that actually serves ---------------------------------
if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    # NB: probed with `systemctl show -p LoadState`, not `systemctl cat` (which
    # SIGPIPEs its pager and returns 141) and not `list-unit-files | grep -q`
    # (which SIGPIPEs systemctl under `set -o pipefail`).
    unit=""
    for u in tftpd-hpa.service tftp.socket tftp.service; do
        if [ "$(systemctl show -p LoadState --value "$u" 2>/dev/null)" = loaded ]; then
            unit="$u"
            break
        fi
    done
    if [ -z "$unit" ]; then
        echo "tftpd-hpa-anon-write: no tftpd-hpa/tftp systemd unit found after install" >&2
        exit 1
    fi
    systemctl enable "$unit" >/dev/null 2>&1 || true
    if systemctl is-active --quiet "$unit"; then
        # enable --now does NOT re-exec an already-running unit, so the
        # postinst-started daemon would keep the old options without this.
        systemctl restart "$unit"
    else
        systemctl start "$unit"
    fi
elif command -v rc-service >/dev/null 2>&1 && rc-service tftpd-hpa status >/dev/null 2>&1; then
    rc-service tftpd-hpa restart
fi

# ---- 4. assert the finding is live ------------------------------------------
# The probe lands in the world-readable service root, so it is removed on every
# exit path and carries a neutral name -- a leftover `...-anon-write-probe.txt`
# would name the plant to any blue-teamer who lists the TFTP root.
probe=.tftpd-write-test
cleanup_probe() {
    rm -f "$root/$probe" "/tmp/$probe" "/tmp/$probe.get"
}
trap cleanup_probe EXIT
printf 'tftpd write test\n' >"/tmp/$probe"

client=""
if command -v curl >/dev/null 2>&1; then
    client=curl
elif command -v tftp >/dev/null 2>&1; then
    client=tftp
fi

uploaded=1
if [ "$client" = curl ]; then
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if curl -sS --max-time 5 -T "/tmp/$probe" "tftp://127.0.0.1/$probe" >/dev/null 2>&1; then
            uploaded=0
            break
        fi
        sleep 1
    done
elif [ "$client" = tftp ]; then
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if tftp 127.0.0.1 -c put "/tmp/$probe" "$probe" >/dev/null 2>&1; then
            uploaded=0
            break
        fi
        sleep 1
    done
fi

if [ "$client" = curl ] && [ "$uploaded" -eq 0 ]; then
    if ! curl -sS --max-time 5 "tftp://127.0.0.1/$probe" -o"/tmp/$probe.get" ||
        ! grep -q 'tftpd write test' "/tmp/$probe.get"; then
        echo "tftpd-hpa-anon-write: anonymous TFTP upload succeeded but the read-back did not match" >&2
        exit 1
    fi
elif [ "$client" = tftp ] && [ "$uploaded" -eq 0 ]; then
    rm -f "/tmp/$probe.get"
    if ! tftp 127.0.0.1 -c get "$probe" "/tmp/$probe.get" >/dev/null 2>&1 ||
        ! grep -q 'tftpd write test' "/tmp/$probe.get"; then
        echo "tftpd-hpa-anon-write: anonymous TFTP upload succeeded but the read-back did not match" >&2
        exit 1
    fi
elif [ "$uploaded" -ne 0 ] && [ -n "$client" ]; then
    echo "tftpd-hpa-anon-write: anonymous TFTP WRITE failed -- the plant is not live" >&2
    exit 1
else
    # No client on the box: fall back to a structural assertion, loudly.
    echo "tftpd-hpa-anon-write: no curl/tftp client present; asserting options structurally" >&2
    case "$(stat -c %a "$root" 2>/dev/null || echo '?')" in
        777 | 1777) ;;
        *)
            echo "tftpd-hpa-anon-write: TFTP root $root is not world-writable" >&2
            exit 1
            ;;
    esac
fi

