#!/bin/bash
# One-shot Alpine cloud-init template builder. Run ON the Proxmox host as root:
#   ssh root@<node> 'bash -s' < build_alpine_ci_template.sh
# Unlike base-fedora44-fix (never booted — Fedora Cloud ships everything), the Alpine
# cloud image lacks the packages the pipeline's Linux paths need on the guest, so this
# boots ONCE with a cicustom bootstrap snippet, installs them, wipes cloud-init state,
# and seals the template:
#   bash            — nakon bundles are bash scripts
#   shadow          — useradd/chpasswd for credlist accounts (fix_services_on_boxes)
#   sudo            — setup_ubuntu_auth + nakon authenticate with NOPASSWD sudoers
#   qemu-guest-agent — _box_settled / prep / guest-agent fallback all use the agent
#   cloud-utils-growpart + e2fsprogs — rootfs grow on first clone boot
set -euo pipefail

NEWID=${NEWID:-1017}
NAME=${NAME:-base-alpine3.23-fix}
STORAGE=${STORAGE:-local-lvm}
AVER=${AVER:-3.23}
BOOTIP=${BOOTIP:-10.0.0.249}
WORK=/root/alpine-ci-build

CLOUD_BASE="https://dl-cdn.alpinelinux.org/alpine/v${AVER}/releases/cloud"
SNIPPET=/var/lib/vz/snippets/alpine-ci-bootstrap-${NEWID}.yaml

echo "== Alpine CI template builder: vmid ${NEWID} name ${NAME} storage ${STORAGE}"
if qm status "$NEWID" >/dev/null 2>&1; then
    echo "vmid ${NEWID} already exists — pick another NEWID" >&2
    exit 1
fi

mkdir -p "$WORK" /var/lib/vz/snippets
cd "$WORK"

echo "== Resolving latest x86_64 BIOS cloudinit image for v${AVER}"
IMG=$(curl -fsSL "${CLOUD_BASE}/" | grep -oE "alpine-[0-9.]+-x86_64-bios-cloudinit-r0\.qcow2" | sort -V | tail -1)
[ -n "$IMG" ] || { echo "no cloudinit qcow2 found at ${CLOUD_BASE}/" >&2; exit 1; }
echo "== Image: ${IMG}"
[ -f "$IMG" ] || curl -fSL -o "$IMG" "${CLOUD_BASE}/${IMG}"
curl -fsSL -o "${IMG}.sha512" "${CLOUD_BASE}/${IMG}.sha512"
echo "$(tr -d '[:space:]' < "${IMG}.sha512")  ${IMG}" | sha512sum -c -

echo "== Creating VM shell"
qm create "$NEWID" --name "$NAME" --memory 2048 --cores 1 --cpu host \
    --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-single \
    --serial0 socket --vga serial0 --agent enabled=1 --ostype l26

echo "== Importing disk"
qm importdisk "$NEWID" "$IMG" "$STORAGE"
qm set "$NEWID" --scsi0 "${STORAGE}:vm-${NEWID}-disk-0,discard=on"
qm set "$NEWID" --boot order=scsi0
qm disk resize "$NEWID" scsi0 15G || true

echo "== Cloud-init drive + bootstrap snippet"
qm set "$NEWID" --ide2 "${STORAGE}:cloudinit" --ciupgrade 0
cat > "$SNIPPET" <<'YAML'
#cloud-config
package_update: true
packages: [bash, shadow, sudo, qemu-guest-agent, cloud-utils-growpart, e2fsprogs]
# Alpine's cloud-init ENI renderer writes dns-nameservers into
# /etc/network/interfaces but NOTHING creates /etc/resolv.conf (no resolvconf
# hook), so apk's index fetch dies of DNS timeouts and `packages:` silently
# no-ops (live-found 2026-09-27). manage_resolv_conf writes the file directly,
# and it runs before the package module (init/config stage vs final).
manage_resolv_conf: true
resolv_conf:
  nameservers: ["8.8.8.8"]
runcmd:
  - rc-update add qemu-guest-agent default
  - rc-service qemu-guest-agent start || true
YAML
qm set "$NEWID" --cicustom "user=local:snippets/alpine-ci-bootstrap-${NEWID}.yaml"
qm set "$NEWID" --ipconfig0 "ip=${BOOTIP}/24,gw=10.0.0.1" --nameserver 8.8.8.8

echo "== Booting for one-time bootstrap"
qm start "$NEWID"

echo -n "== Waiting for guest agent"
for i in $(seq 1 60); do
    if qm agent "$NEWID" ping >/dev/null 2>&1; then echo " up (${i}0s)"; break; fi
    echo -n .
    sleep 10
    if [ "$i" -eq 60 ]; then echo " TIMEOUT" >&2; exit 1; fi
done

echo "== Waiting for cloud-init to finish"
for i in $(seq 1 24); do
    st=$(qm guest exec "$NEWID" -- cloud-init status 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["out-data"])' 2>/dev/null || true)
    case "$st" in
        *"status: done"*) echo "cloud-init done"; break;;
        *"error"*) echo "cloud-init ERROR" >&2; exit 1;;
    esac
    sleep 5
    if [ "$i" -eq 24 ]; then echo "cloud-init never finished (last: ${st:-<none>})" >&2; exit 1; fi
done

echo "== Verifying bootstrap packages"
# qm guest exec exits 0 even when the guest command fails — gate on the JSON
# exitcode, or a broken bootstrap (missing shadow/sudo) seals silently.
# Check binaries ONE per command -v: busybox ash's multi-arg `command -v`
# exits 2 even when everything is installed (live-found 2026-09-28 — the
# original run "passed" only because nothing looked at the exit code).
VERIFY_OUT=$(qm guest exec "$NEWID" -- sh -c '
    for b in bash useradd chpasswd sudo rc-service; do
        command -v "$b" >/dev/null || { echo "MISSING:$b"; exit 1; }
    done
    apk info -e qemu-guest-agent cloud-utils-growpart e2fsprogs || exit 1
    cloud-init status --long' 2>/dev/null)
echo "$VERIFY_OUT"
echo "$VERIFY_OUT" | python3 -c 'import json,sys; exit(json.load(sys.stdin)["exitcode"])' || {
    echo "bootstrap verification FAILED — packages missing, refusing to seal" >&2
    exit 1
}

echo "== Wiping cloud-init state and sealing"
qm guest exec "$NEWID" -- cloud-init clean --logs --seed
sleep 2
qm guest exec "$NEWID" -- poweroff
for i in $(seq 1 30); do
    qm wait "$NEWID" --timeout 5 >/dev/null 2>&1 && break
    sleep 2
done
qm set "$NEWID" --delete cicustom --delete ipconfig0 --delete nameserver
rm -f "$SNIPPET"
qm set "$NEWID" --tags "cloud-init;general;template"
qm template "$NEWID"

echo "== Done: ${NAME} = vmid ${NEWID} (template, stopped)"
qm config "$NEWID"
