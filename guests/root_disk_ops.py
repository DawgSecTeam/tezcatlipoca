"""Growing a guest's root filesystem to fill its enlarged disk.

There were two of these (a golden one and an engine one) with different device parsing: the
engine's version silently failed on btrfs and on a late `/dev/dm-N`, which is how a 40 GB
disk carried a 10 GB root into an event. One script now handles plain, LVM, btrfs and xfs.
"""

import shlex

# cloud-init only expands a PLAIN partition+fs; LVM keeps its original root LV and btrfs needs
# its own grow. `/dev/sda3[/root]` (btrfs) also breaks a partition-digit parse, hence the
# bracket strip. Every step is best-effort: a genuinely undersized root fails loudly at the
# first big plant, which is a better error than a deploy that dies here.
GROW_ROOT_SCRIPT = (
    "ROOT_SRC=$(findmnt -no SOURCE / 2>/dev/null); "
    "SRC=${ROOT_SRC%%\\[*}; "
    # Golden clones can run with the LV mounted but no /dev/dm-N node (udev late under load).
    "command -v dmsetup >/dev/null 2>&1 && dmsetup mknodes 2>/dev/null; "
    "if command -v growpart >/dev/null 2>&1; then "
    "case \"$SRC\" in "
    "/dev/mapper/*|/dev/dm-*) "
    "PV=$(pvs --noheadings -o pv_name 2>/dev/null | tr -d ' ' | head -1); "
    "DISK=$(basename \"$PV\" | sed 's/[0-9]*$//'); "
    "PART=$(basename \"$PV\" | grep -o '[0-9]*$'); "
    "if [ -n \"$DISK\" ] && [ -n \"$PART\" ]; then growpart \"/dev/$DISK\" \"$PART\" >/dev/null 2>&1; fi; "
    "pvresize \"$PV\" >/dev/null 2>&1; "
    "LV=$(lvs --noheadings -o lv_path 2>/dev/null | tr -d ' ' | head -1); "
    "lvextend -l +100%FREE \"$LV\" >/dev/null 2>&1; "
    ";; "
    "/dev/*) "
    "DISK=$(basename \"$SRC\" | sed 's/[0-9]*$//'); "
    "PART=$(basename \"$SRC\" | grep -o '[0-9]*$'); "
    "if [ -n \"$DISK\" ] && [ -n \"$PART\" ]; then growpart \"/dev/$DISK\" \"$PART\" >/dev/null 2>&1; fi; "
    ";; "
    "esac; "
    "fi; "
    "case \"$(stat -f -c %T / 2>/dev/null)\" in "
    "btrfs) btrfs filesystem resize max / >/dev/null 2>&1;; "
    "xfs) xfs_growfs / >/dev/null 2>&1;; "
    "*) resize2fs \"$SRC\" >/dev/null 2>&1;; "
    "esac; "
    "df -h / | awk 'NR==2 {print \"root: \"$2\" total, \"$4\" free\"}'"
)


def grow_root_command():
    """The script as one root shell command (callers run it over SSH or the guest agent)."""
    return f"sudo -H sh -c {shlex.quote(GROW_ROOT_SCRIPT)}"
