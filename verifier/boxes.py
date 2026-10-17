"""Machine-list helpers shared by every gate that picks boxes out of nakon-config.json."""

from common.windows_ops import is_windows_template


def is_linux_box(box):
    return not is_windows_template(str(box.get("os") or ""))


def boxes_by_team(boxes):
# {team identifier: [box, ...]}, keyed on the IP's third octet (192.168.<id>.<host>); a box with no usable IP is skipped, not guessed.
    out = {}
    for b in boxes or []:
        ip = str(b.get("ip") or "").strip()
        parts = ip.split(".")
        if len(parts) != 4 or not parts[2].isdigit():
            continue
        out.setdefault(parts[2], []).append(b)
    return out
