"""Golden root-disk sizing and in-guest root filesystem expansion."""

import re
import shlex
import time

from range_ops import proxmox_api
from ssh_ops import ssh_via_gateway
from utils import record_degradation


_SIZE_TO_GB = {"": 1 / 1024 ** 3, "K": 1 / 1024 ** 2, "M": 1 / 1024, "G": 1, "T": 1024}


def _root_disk_gb(cfg, vmid):
    """(config key, size in GB) of the clone's root disk, or (None, 0).

    The bus varies per base template (scsi0 on the -fix images); cdrom and
    cloud-init entries are not disks. Parsed from the config because the
    clone inherits the template's size verbatim — there is no other record
    of it (svc-matrix: the 15 GB ubuntu template disk filled mid-plant on
    splunk's .deb unpack while team clones got their terraform disk_gb)."""
    for key in sorted(k for k in cfg
                      if k.startswith(("scsi", "virtio", "sata", "ide"))):
        val = cfg[key]
        if "media=cdrom" in val or "cloudinit" in val or "size=" not in val:
            continue
        m = re.search(r"size=(\d+(?:\.\d+)?)([KMGT]?)", val)
        if m:
            return key, int(float(m.group(1)) * _SIZE_TO_GB[m.group(2)])
    return None, 0


def ensure_golden_disk_size(node, vmid, disk_gb):
    """Grow the golden clone's root disk to the box's disk_gb BEFORE first boot.

    Golden clones inherit the base template's disk verbatim (terraform only
    sizes the team clones), so a big plant payload (splunk) can fill the
    template-sized disk mid-golden-build. Grow-only — a shrink would destroy
    data — and before start_vm. Guest-side expansion is a separate post-boot
    step (expand_guest_root_disks): cloud-init only grows a plain partition+fs,
    so LVM layouts need growpart + pvresize + lvextend + resize2fs."""
    if not disk_gb:
        return
    cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    key, cur_gb = _root_disk_gb(cfg, vmid)
    if key is None:
        print(f"    WARNING: golden {vmid} has no root disk — disk_gb={disk_gb} not applied")
        return
    if disk_gb <= cur_gb:
        return
    proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/resize",
                data={"disk": key, "size": f"{disk_gb}G"})
    print(f"    golden {vmid}: {key} grown {cur_gb}G -> {disk_gb}G")


def expand_guest_root_disks(targets, ctx):
    """Guest-side companion to ensure_golden_disk_size: cloud-init only expands a
    PLAIN partition+fs on first boot — LVM layouts (ubuntu cloud images) keep their
    original root LV, and btrfs layouts (default on Fedora cloud images — live-found
    2026-09-29: findmnt reports '/dev/sda3[/root]', whose bracketed subvolume suffix
    broke the partition-digit parse AND resize2fs can't grow btrfs) need their own
    grow. growpart + pvresize + lvextend / btrfs resize / xfs_growfs / resize2fs
    after first boot; every step no-ops when there is nothing to grow, and boxes
    without growpart (Alpine) are skipped. Windows goldens are out of scope (they
    boot at their template's own size)."""
    script = (
        "command -v growpart >/dev/null || { echo 'growpart unavailable - skipping'; exit 0; }; "
        "set -e; "
        # base-ubuntu24.04-fix golden clones can run MINUTES with the LV mounted but
        # no /dev/dm-N node (udev late under boot load — live-found 2026-09-29:
        # resize2fs "No such file or directory" on the live root LV at T+40s, node
        # present at T+16min). dmsetup mknodes materializes the node immediately.
        "command -v dmsetup >/dev/null 2>&1 && dmsetup mknodes 2>/dev/null || true; "
        "ROOT_SRC=$(findmnt -no SOURCE /); "
        "SRC=${ROOT_SRC%%\\[*}; "  # strip a btrfs subvolume suffix: /dev/sda3[/root] -> /dev/sda3
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
        # The grow itself is retried across the whole boot window: a fresh golden's SSH
        # can be dark for MINUTES under node load (live-found 2026-10-04, the scrim-one
        # validation: 3x15s of retries all landed inside the dark window, the expansion
        # was skipped, and splunk01's plant then died on ENOSPC). Escalating backoff
        # spans ~6.5 min; each attempt is also the SSH-wait, so no separate probe phase
        # is needed.
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
            # Expansion is an ENOSPC guard, not a correctness gate. Fail ONLY when the
            # root fs is measurably too small for the plants (the regression-4x1 shape:
            # a 10G LV inside a 30G disk). When the size can't be measured — the same
            # boot-window instability that broke the grow usually breaks the probe too
            # (live-found 2026-09-29 x3) — warn and continue; a genuinely undersized
            # root dies loudly at first big plant, named by the plant coverage gate.
            # The probe gets the SAME boot-window budget as the grow: a short retry
            # inherited the flakiness and recorded a permanent "size unmeasurable"
            # degradation on healthy boxes (2026-10-03, 2026-10-04).
            size_gb = 0.0
            for attempt, backoff in enumerate((15, 30, 45, 60, 60, 60, 60), 1):
                try:
                    probe = ssh_via_gateway(ctx, t["ip"], "df -BK / | awk 'NR==2{print $2}'",
                                            timeout=60, user=ctx.get("box_username", "ubuntu"))
                    size_gb = int((probe.stdout or "0").strip().splitlines()[-1]) / 1024 ** 2
                    if size_gb:
                        break
                except (ValueError, IndexError, Exception):
                    size_gb = 0.0
                if attempt < 7:
                    time.sleep(backoff)
            need_gb = max(int(t.get("disk_gb") or 10) - 4, 1)
            if size_gb and size_gb < need_gb:
                raise RuntimeError(f"root-disk too small on golden {t['ip']} "
                                   f"({size_gb:.0f}G < {need_gb}G) and expansion failed "
                                   f"(rc={r.returncode}): {(r.stderr or out).strip()[:200]}")
            if not size_gb:
                record_degradation("golden root-disk expansion failed",
                                   f"{t['ip']}: size unmeasurable")
                print(f"    {t['ip']}: WARNING expansion failed (size unmeasurable) — "
                      f"continuing; first big plant will surface a truly undersized root")
            else:
                print(f"    {t['ip']}: expansion failed but root measures "
                      f"{size_gb:.0f}G >= {need_gb}G — continuing without a degradation "
                      f"(probe succeeded after retry; disk is big enough for the plants)")
        df_lines = [l for l in out.splitlines() if l.startswith("/dev/")]
        print(f"    {t['ip']}: root-disk expansion done"
              + (f" ({df_lines[-1].split()[2] if len(df_lines[-1].split()) > 2 else 'used=?'} used)" if df_lines else ""))
