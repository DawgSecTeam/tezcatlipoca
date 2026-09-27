#!/usr/bin/env bash
# Build a cloud-init-ready Fedora base template on cyberfield (node pve), mirroring the
# structure of 1006 base-debian13-lite-fix / 1007 base-ubuntu24.04-fix (tags
# cloud-init;general;template). The official Fedora Cloud Base qcow2 already ships cloud-init,
# so no in-guest install is needed — we import the disk, attach a cloud-init drive + serial
# console, and convert to a template.
#
# Also re-tags 1008 base-windows-server with `template` (it lost the tag; preflight needs it).
#
# RUN ON THE HOST:  ssh root@10.0.0.193 'bash -s' < build_fedora_ci_template.sh
# or scp it over and `bash build_fedora_ci_template.sh`.
#
# Requires host access (this is the step blocked by the auto-mode classifier from the agent).
set -euo pipefail

NEWID="${NEWID:-1015}"                 # free vmid for the new template (verify before run)
NAME="${NAME:-base-fedora44-fix}"
STORAGE="${STORAGE:-local-lvm}"        # where the disk lands
WORK=/root/fedora-ci-build
FVER="${FVER:-44}"

mkdir -p "$WORK"; cd "$WORK"

# 1) Resolve + fetch the Fedora Cloud Base Generic qcow2 (cloud-init preinstalled).
BASE="https://download.fedoraproject.org/pub/fedora/linux/releases/${FVER}/Cloud/x86_64/images/"
if [ ! -f fedora-cloud.qcow2 ]; then
  echo "Resolving Fedora ${FVER} Cloud Base image..."
  FILE=$(curl -fsSL "$BASE" | grep -oE "Fedora-Cloud-Base-Generic[^\"]*\.x86_64\.qcow2" | head -1 || true)
  [ -z "$FILE" ] && FILE=$(curl -fsSL "$BASE" | grep -oE "Fedora-Cloud-Base[^\"]*\.qcow2" | head -1)
  [ -z "$FILE" ] && { echo "Could not find a Fedora ${FVER} cloud qcow2 under $BASE"; exit 1; }
  echo "Downloading $FILE"
  curl -fSL "${BASE}${FILE}" -o fedora-cloud.qcow2
fi

# 2) Create the VM shell (q35 + serial console, virtio, agent on).
qm status "$NEWID" >/dev/null 2>&1 && { echo "vmid $NEWID already exists — pick another NEWID"; exit 1; }
qm create "$NEWID" --name "$NAME" --memory 2048 --cores 1 --cpu host \
  --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-single \
  --serial0 socket --vga serial0 --agent enabled=1 --ostype l26

# 3) Import the disk and attach it.
qm importdisk "$NEWID" fedora-cloud.qcow2 "$STORAGE"
qm set "$NEWID" --scsi0 "${STORAGE}:vm-${NEWID}-disk-0"
qm set "$NEWID" --boot order=scsi0

# 4) Cloud-init drive + defaults (identity is injected per-clone by tezcatlipoca at deploy).
qm set "$NEWID" --ide2 "${STORAGE}:cloudinit"
qm set "$NEWID" --ciupgrade 0
qm disk resize "$NEWID" scsi0 15G || true

# 5) Tag + convert to template (matches 1006/1007).
qm set "$NEWID" --tags "cloud-init;general;template"
qm template "$NEWID"
echo "Built template $NEWID ($NAME)."

# 6) Restore the template tag on the Windows base (preflight requires it).
CUR=$(qm config 1008 | awk -F': ' '/^tags:/{print $2}')
case ";$CUR;" in *";template;"*) echo "1008 already tagged template";; *)
  qm set 1008 --tags "${CUR:+$CUR;}template"; echo "re-tagged 1008 -> ${CUR:+$CUR;}template";;
esac
echo "DONE. Now: app01 uses template '$NAME' (boxes.json updated)."
