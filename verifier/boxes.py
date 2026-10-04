"""Machine-list helpers shared by every gate that picks boxes out of nakon-config.json."""

from windows_ops import is_windows_template


def is_linux_box(box):
    """True unless the machine entry's `os` says Windows (the repo's single "win" rule)."""
    return not is_windows_template(str(box.get("os") or ""))


def boxes_by_team(boxes):
    """{team identifier: [box, ...]} from the machine list, keyed on the IP's third
    octet — the same convention every other gate uses (`192.168.<id>.<host>`).

    A box with no usable IP is skipped rather than guessed at; a half-edited machine
    list must not silently shrink the set of teams a gate proves."""
    out = {}
    for b in boxes or []:
        ip = str(b.get("ip") or "").strip()
        parts = ip.split(".")
        if len(parts) != 4 or not parts[2].isdigit():
            continue
        out.setdefault(parts[2], []).append(b)
    return out
