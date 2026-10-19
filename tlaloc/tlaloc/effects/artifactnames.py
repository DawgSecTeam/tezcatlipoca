"""Randomised artifact names for the persistence families: every plant draws one fresh naming theme from these pools — a fixed name is an event-long IOC for blue (`naming.mode`: full|stem|suffix)."""

import random
import re

LINUX_STEMS = [
    "cupsd", "cups-browsed", "udisks2", "colord", "accounts-daemon",
    "packagekit", "packagekit-offline", "upower", "thermald", "irqbalance",
    "smartd", "fwupd", "bolt", "modemmanager", "networkd", "timesyncd",
    "chrony", "logind", "resolved", "coredump", "oomd", "polkit", "geoclue",
    "flatpak", "snapd", "sssd", "auditd", "crond", "atd", "gssproxy",
    "wpa-supplicant", "bluetooth", "pipewire", "wireplumber", "avahi", "gvfs",
    "rtkit", "kerneloops", "abrtd", "setroubleshootd", "fprintd",
    "switcheroo-control", "power-profiles-daemon", "iio-sensor-proxy",
    "libvirt", "virtlogd", "multipathd", "iscsid", "dm-event", "lvmpolld",
    "udev", "rsyslogd", "logrotate", "systemd-tmpfiles", "dbus-broker", "cups",
]

# Invented stems only: none may collide with a real Microsoft service.
WIN_STEMS = [
    "hvdiag", "csreport", "wmitrace", "devnode", "pwrshift", "sysvault",
    "regpoll", "shdwcopy", "netshield", "perfhub", "winsvc", "drvstore",
    "hlthmon", "clusterdiag", "evtxsrv", "taskwatch", "secagent", "updguard",
    "cacheman", "devman", "logrelay", "netmeter", "perfwiz", "policysync",
    "resmon", "schedguard", "sigstore", "sysinfo", "timekeeper", "wmihelpr",
]

PREFIXES = ["20", "30", "40", "50", "60", "70", "80", "90", "95"]

MODE_FULL, MODE_STEM, MODE_SUFFIX = "full", "stem", "suffix"
VALID_MODES = (MODE_FULL, MODE_STEM, MODE_SUFFIX)

SUFFIX_POOL = [
    "refresh", "renew", "watchdog", "updater", "helper", "monitor", "guard",
    "relay", "keeper", "sync", "stamp", "sweep", "prune", "agent", "probe",
    "warden", "bridge", "runner", "store", "collector", "harvest", "digest",
]
SUFFIX_SET = set(SUFFIX_POOL)

# Accounts are svc-<label>: no pool word may collide with a credlist name
# (svc-backup must stay rotatable) or appear as filler in a description or path.
STEMS = set(LINUX_STEMS) | set(WIN_STEMS)
ACCOUNT_LABELS = STEMS | SUFFIX_SET

USED_NAMES_KEY = "artifact_names"
USED_SUFFIXES_KEY = "artifact_suffixes"


def mode_from_cfg(cfg):
    mode = ((cfg or {}).get("naming") or {}).get("mode")
    return mode if mode in VALID_MODES else MODE_FULL


def is_planted_account(user):
    """A planted svc-* account, never a credlist one; a stem may itself contain
    a hyphen, so every split of the label is checked."""
    user = (user or "").strip()
    if not user.startswith("svc-"):
        return False
    rest = user[4:]
    if not rest:
        return False
    if rest in ACCOUNT_LABELS:
        return True
    parts = rest.split("-")
    for split in range(1, len(parts)):
        stem = "-".join(parts[:split])
        tail = "-".join(parts[split:])
        if stem in STEMS and (tail in SUFFIX_SET or tail in STEMS):
            return True
    return False


def used_names(world):
    return set((world.meta.get(USED_NAMES_KEY) or [])) if world is not None else set()


def _used(world, key):
    return set((world.meta.get(key) or [])) if world is not None else set()


def _remember(world, *labels, key=USED_NAMES_KEY):
    if world is None:
        return
    names = list(world.meta.get(key) or [])
    names.extend(x for x in labels if x)
    world.meta[key] = names


def draw(world, pool, rng=None, key=USED_NAMES_KEY):
    rng = rng or random
    free = [s for s in pool if s not in _used(world, key)]
    value = rng.choice(free or list(pool))
    _remember(world, value, key=key)
    return value


def _draw_suffixes(world, count, rng=None):
    rng = rng or random
    free = [s for s in SUFFIX_POOL if s not in _used(world, USED_SUFFIXES_KEY)]
    rng.shuffle(free)
    out = free[:count]
    while len(out) < count:                  # exhausted: recycle, still distinct
        extra = [s for s in SUFFIX_POOL if s not in out]
        rng.shuffle(extra)
        out.extend(extra[:count - len(out)])
    _remember(world, *out, key=USED_SUFFIXES_KEY)
    return out


def _label(stem, suffix, mode):
    if mode == MODE_STEM:
        return stem
    if mode == MODE_SUFFIX:
        return suffix
    return f"{stem}-{suffix}"


def title(word):
    return "".join(part[:1].upper() + part[1:]
                   for part in word.replace("_", "-").split("-"))


def _labels_for(world, pool, roles, mode, rng):
    if mode == MODE_STEM:
        return [draw(world, pool, rng) for _ in range(roles)]
    if mode == MODE_SUFFIX:
        return _draw_suffixes(world, roles, rng)
    stem = draw(world, pool, rng)
    return [f"{stem}-{s}" for s in _draw_suffixes(world, roles, rng)]


# Roles share namespaces (three files in /etc/cron.d, three units in one):
# every draw must give each role a DISTINCT label.
LINUX_ROLES = ("unit", "timer", "decoy", "cron", "reaper", "clean", "user",
               "keyid", "profile", "hider", "bait")

WINDOWS_ROLES = ("run_value", "task", "log_task", "user", "service", "wmi", "decoy")


def linux_names(world, rng=None, mode=MODE_FULL):
    rng = rng or random
    labels = _labels_for(world, LINUX_STEMS, len(LINUX_ROLES), mode, rng)
    (unit, timer, decoy, cron, reaper, clean, user, keyid, profile_id,
     hider, bait) = labels
    nn = rng.choice(PREFIXES)
    return {
        "stem": labels[0].split("-")[0],
        "mode": mode,
        "profile": f"/etc/profile.d/{nn}-{profile_id}-env.sh",
        "sshd_dropin": f"/etc/ssh/sshd_config.d/{nn}-{profile_id}.conf",
        "keys_dir": f"/etc/ssh/.{keyid}-keys",
        "cron": f"/etc/cron.d/{cron}",
        "cron_reaper": f"/etc/cron.d/{reaper}",
        "cron_marker": f"/var/tmp/.{clean}",
        "unit": f"{unit}.service",
        "unit_desc": f"{title(unit)} Service",
        "timer_unit": f"{timer}.service",
        "timer": f"{timer}.timer",
        "suid": f"/usr/lib/.{keyid}d",
        "user": f"svc-{user}",
        "key_comment": keyid,
        "log_cleanup": f"/etc/cron.d/{clean}",
        "decoy_unit": f"{decoy}.service",
        "decoy_path": f"/opt/{decoy}/{decoy}.sh",
        # hider and bait carry distinct labels: a box wearing both persist
        # families must not have one plant overwrite the other's directory.
        "hider_dir": f"/usr/lib/.{hider}",
        "hider_so": f"/usr/lib/.{hider}/lib{hider}.so",
        "hider_conf": f"/etc/.{hider}.conf",
        "bait_lib": f"/usr/local/lib/lib{bait}.so",
        "bait_dir": f"/usr/local/lib/.{bait}",
        "bait_conf": f"/usr/local/lib/.{bait}/hide.conf",
        "bait_pid": f"/usr/local/lib/.{bait}/decoy.pid",
        "bait_state": f"/var/lib/.{bait}-prev",
        "bait_staging": f"/tmp/.{bait}-upload",
    }


def windows_names(world, rng=None, mode=MODE_FULL):
    rng = rng or random
    labels = _labels_for(world, WIN_STEMS, len(WINDOWS_ROLES), mode, rng)
    run_value, task, log_task, user, service, wmi, decoy = labels
    return {
        "stem": labels[0].split("-")[0],
        "mode": mode,
        "run_value": title(run_value),
        "task": title(task),
        "log_task": title(log_task),
        "user": f"svc-{user}",
        "service": title(service),
        "script": f"C:\\Windows\\System32\\{run_value}.cmd",
        "wmi": title(wmi),
        "decoy_service": title(decoy),
        "decoy_script": f"C:\\Windows\\System32\\{decoy}.cmd",
    }


# Port-block names are drawn per activation so the undo recipe names THIS
# block's table/rule — a second block on one box cannot collide with the first.
BLOCK_ROLES_LINUX = ("table", "chain")
BLOCK_ROLES_WINDOWS = ("rule",)


def _nft_safe(word):
    """nft identifiers are [A-Za-z0-9_]; a hyphenated stem is a syntax error."""
    return re.sub(r"[^A-Za-z0-9_]", "_", word or "")


def block_names(world, rng=None, mode=MODE_FULL, windows=False):
    rng = rng or random
    if windows:
        labels = _labels_for(world, WIN_STEMS, len(BLOCK_ROLES_WINDOWS), mode, rng)
        return {"rule": title(labels[0]).replace("_", ""), "mode": mode}
    labels = _labels_for(world, LINUX_STEMS, len(BLOCK_ROLES_LINUX), mode, rng)
    table, chain = labels
    return {"table": _nft_safe(table), "chain": _nft_safe(chain), "mode": mode}
