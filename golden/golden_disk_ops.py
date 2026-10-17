"""Golden root-disk sizing and in-guest root filesystem expansion."""

import re
import shlex
import time

from infra.client import proxmox_api
from common.ssh_ops import ssh_via_gateway
from common.utils import record_degradation


_SIZE_TO_GB = {"": 1 / 1024 ** 3, "K": 1 / 1024 ** 2, "M": 1 / 1024, "G": 1, "T": 1024}


# Parsed from the config: the clone inherits the template's size verbatim, with no other record.
def _root_disk_gb(cfg):
    for key in sorted(k for k in cfg
                      if k.startswith(("scsi", "virtio", "sata", "ide"))):
        val = cfg[key]
        if "media=cdrom" in val or "cloudinit" in val or "size=" not in val:
            continue
        m = re.search(r"size=(\d+(?:\.\d+)?)([KMGT]?)", val)
        if m:
            return key, int(float(m.group(1)) * _SIZE_TO_GB[m.group(2)])
    return None, 0


# The create path only sizes team clones, so a big plant payload (splunk) can fill the
# template-sized golden disk mid-build. Grow-only, and before start_vm.
def ensure_golden_disk_size(node, vmid, disk_gb):
    if not disk_gb:
        return
    cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    key, cur_gb = _root_disk_gb(cfg)
    if key is None:
        print(f"    WARNING: golden {vmid} has no root disk — disk_gb={disk_gb} not applied")
        return
    if disk_gb <= cur_gb:
        return
    proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/resize",
                data={"disk": key, "size": f"{disk_gb}G"})
    print(f"    golden {vmid}: {key} grown {cur_gb}G -> {disk_gb}G")


# cloud-init only expands a PLAIN partition+fs: LVM keeps its original root LV and btrfs needs
# its own grow ('/dev/sda3[/root]' also breaks the partition-digit parse). Alpine/Windows skipped.
def expand_guest_root_disks(targets, ctx):
    script = (
        "command -v growpart >/dev/null || { echo 'growpart unavailable - skipping'; exit 0; }; "
        "set -e; "
        # Golden clones can run minutes with the LV mounted but no /dev/dm-N node (udev
        # late under load), so resize2fs fails; dmsetup mknodes materializes it.
        "command -v dmsetup >/dev/null 2>&1 && dmsetup mknodes 2>/dev/null || true; "
        "ROOT_SRC=$(findmnt -no SOURCE /); "
        "SRC=${ROOT_SRC%%\\[*}; "
        "case \"$(stat -f -c %T /)\" in "
        "btrfs) GROW_FS=\"btrfs filesystem resize max /\";; "
        "xfs) GROW_FS=\"xfs_growfs /\";; "
        "*) GROW_FS=\"resize2fs $SRC\";; "
        "esac; "
        "case \"$SRC\" in "
        "/dev/mapper/*|/dev/dm-*) "
        "PV=$(pvs --noheadings -o pv_name | tr -d ' ' | head -1); "
        "DISK=$(basename \"$PV\" | sed 's/[0-9]*$//'); "
        "PART=$(basename \"$PV\" | grep -o '[0-9]*$'); "
        "if [ -n \"$DISK\" ] && [ -n \"$PART\" ]; then growpart \"/dev/$DISK\" \"$PART\" || true; fi; "
        "pvresize \"$PV\" || true; "
        "LV=$(lvs --noheadings -o lv_path | tr -d ' ' | head -1); "
        "lvextend -l +100%FREE \"$LV\" || true; "
        ";; "
        "/dev/*) "
        "DISK=$(basename \"$SRC\" | sed 's/[0-9]*$//'); "
        "PART=$(basename \"$SRC\" | grep -o '[0-9]*$'); "
        "if [ -n \"$DISK\" ] && [ -n \"$PART\" ]; then growpart \"/dev/$DISK\" \"$PART\" || true; fi; "
        ";; "
        "*) echo 'unrecognized root layout - skipping'; exit 0; ;; "
        "esac; "
        "$GROW_FS; "
        "df -h /"
    )
    for t in targets:
        # Retried across the whole boot window: a fresh golden's SSH can be dark for minutes
        # under load, and a skipped expansion dies later on ENOSPC.
        r = None
        for attempt, backoff in enumerate((15, 30, 45, 60, 60, 60, 60, 60), 1):
            r = ssh_via_gateway(ctx, t["ip"], f"sudo -H sh -c {shlex.quote(script)}",
                                timeout=120, user=ctx.get("box_username", "ubuntu"))
            if r.returncode == 0:
                break
            print(f"    {t['ip']}: root-disk expansion attempt {attempt} failed "
                  f"(rc={r.returncode}) — retrying")
            time.sleep(backoff)
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            # Warn and continue: a genuinely undersized root dies loudly at first big plant.
            record_degradation("golden root-disk expansion failed", f"{t['ip']}: rc={r.returncode}")
            print(f"    {t['ip']}: WARNING expansion failed (rc={r.returncode}) — continuing; "
                  f"first big plant will surface a truly undersized root")
        df_lines = [l for l in out.splitlines() if l.startswith("/dev/")]
        print(f"    {t['ip']}: root-disk expansion done"
              + (f" ({df_lines[-1].split()[2] if len(df_lines[-1].split()) > 2 else 'used=?'} used)" if df_lines else ""))
