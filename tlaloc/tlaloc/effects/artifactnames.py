"""Randomised artifact names for the persistence families.

A fixed name is a giveaway: once blue learns one artifact name they grep it for
the rest of the event, so every draw produces ONE coherent naming theme per
plant and derives every path, unit, account, task and marker from it — the way a
real package names its own files. Names already worn this event are skipped, so
a re-plant wears a fresh face and a pool recycles only when exhausted.

Three modes (`naming.mode`, default `full`) — blue found a plant by
pattern-matching the SUFFIX set rather than the stems, so either half can be
dropped:

  full    ``<stem>-<suffix>``   e.g. ``dbus-broker-refresh.service``
  stem    ``<stem>``            e.g. ``dbus-broker.service`` (no suffix to match)
  suffix  ``<suffix>``          e.g. ``refresh.service``    (no theme linking boxes)

Two design rules that are not negotiable:

* roles that share ONE namespace must draw DISTINCT labels (three files in
  /etc/cron.d, three units) or a later plant overwrites an earlier one;
* a description, path or comment must never contain a pool word as filler —
  that is how a `stem`-mode plant leaked a suffix anyway.
"""

import random
import re

# Linux: distro-plumbing stems, curated so an artifact reads like a package.
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

# Windows: deliberately invented stems — none collide with a real Microsoft
# service (a collision is both noisy and confusing).
WIN_STEMS = [
    "hvdiag", "csreport", "wmitrace", "devnode", "pwrshift", "sysvault",
    "regpoll", "shdwcopy", "netshield", "perfhub", "winsvc", "drvstore",
    "hlthmon", "clusterdiag", "evtxsrv", "taskwatch", "secagent", "updguard",
    "cacheman", "devman", "logrelay", "netmeter", "perfwiz", "policysync",
    "resmon", "schedguard", "sigstore", "sysinfo", "timekeeper", "wmihelpr",
]

# A plausible distro prefix number for drop-in style files, drawn per family.
PREFIXES = ["20", "30", "40", "50", "60", "70", "80", "90", "95"]

MODE_FULL, MODE_STEM, MODE_SUFFIX = "full", "stem", "suffix"
VALID_MODES = (MODE_FULL, MODE_STEM, MODE_SUFFIX)

# The suffix vocabulary (`full`'s second half, `suffix`'s whole name).
SUFFIX_POOL = [
    "refresh", "renew", "watchdog", "updater", "helper", "monitor", "guard",
    "relay", "keeper", "sync", "stamp", "sweep", "prune", "agent", "probe",
    "warden", "bridge", "runner", "store", "collector", "harvest", "digest",
]
SUFFIX_SET = set(SUFFIX_POOL)

# Every account we create is `svc-<label>`, so "is this our own account?" is
# answerable without world state or a fixed literal. Neither pool collides with
# a credlist name (`svc-backup` in particular must stay rotatable).
STEMS = set(LINUX_STEMS) | set(WIN_STEMS)
ACCOUNT_LABELS = STEMS | SUFFIX_SET

USED_NAMES_KEY = "artifact_names"
USED_SUFFIXES_KEY = "artifact_suffixes"


def mode_from_cfg(cfg):
    """`naming.mode` out of a (possibly partial/None) config."""
    mode = ((cfg or {}).get("naming") or {}).get("mode")
    return mode if mode in VALID_MODES else MODE_FULL


def is_planted_account(user):
    """True for an account planted by the persist families (never a credlist one).

    `full` mode names accounts `svc-<stem>-<suffix>`, so the check accepts either
    the whole label or every hyphen-separated part (a stem may itself contain a
    hyphen, e.g. `cups-browsed`). Getting this wrong in either direction is
    expensive: a false positive rotates blue's own credlist account, a false
    negative rotates ours.
    """
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
    """One value from `pool`, skipping anything already worn this event."""
    rng = rng or random
    free = [s for s in pool if s not in _used(world, key)]
    value = rng.choice(free or list(pool))
    _remember(world, value, key=key)
    return value


def _draw_suffixes(world, count, rng=None):
    """`count` DISTINCT suffix words (one box never wears `refresh` twice)."""
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
    """`dbus-broker-refresh` -> `DbusBrokerRefresh` (a plausible unit/task name)."""
    return "".join(part[:1].upper() + part[1:]
                   for part in word.replace("_", "-").split("-"))


def _labels_for(world, pool, roles, mode, rng):
    if mode == MODE_STEM:
        return [draw(world, pool, rng) for _ in range(roles)]
    if mode == MODE_SUFFIX:
        return _draw_suffixes(world, roles, rng)
    stem = draw(world, pool, rng)
    return [f"{stem}-{s}" for s in _draw_suffixes(world, roles, rng)]


# -- the per-OS name sets -----------------------------------------------------
# Roles are positional and MUST stay in this order: three cron files share
# /etc/cron.d and three units share the unit namespace, so a stem-mode draw
# takes a distinct stem per role (also more realistic — separate packages name
# their own files).

LINUX_ROLES = ("unit", "timer", "decoy", "cron", "reaper", "clean", "user",
               "keyid", "profile", "hider", "bait")

WINDOWS_ROLES = ("run_value", "task", "log_task", "user", "service", "wmi", "decoy")


def linux_names(world, rng=None, mode=MODE_FULL):
    """Every Linux-side name for one family draw."""
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
        # The ld.so.preload hider (persist family `preload`): one hidden
        # directory, the .so inside it and the hide-list conf. Three files in
        # one namespace, drawn from one label — and drawn SEPARATELY from the
        # hunt-bait below, or a box that wears both families would have one
        # plant overwrite the other's directory.
        "hider_dir": f"/usr/lib/.{hider}",
        "hider_so": f"/usr/lib/.{hider}/lib{hider}.so",
        "hider_conf": f"/etc/.{hider}.conf",
        # The procfilter hunt-bait (persist family `decoy`): the filter lib, the
        # hide-list, the decoy's pid file and the state file the next plant
        # reads (it records pid/lib/dir because the filter hides all three from
        # a listing).
        "bait_lib": f"/usr/local/lib/lib{bait}.so",
        "bait_dir": f"/usr/local/lib/.{bait}",
        "bait_conf": f"/usr/local/lib/.{bait}/hide.conf",
        "bait_pid": f"/usr/local/lib/.{bait}/decoy.pid",
        "bait_state": f"/var/lib/.{bait}-prev",
        "bait_staging": f"/tmp/.{bait}-upload",
    }


def windows_names(world, rng=None, mode=MODE_FULL):
    """Every Windows-side name for one family draw."""
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


# -- the port-block name class (block_port) -----------------------------------
# A block that wears a fixed name is an IOC list for the rest of the event: v1
# wore one literal Windows rule (`badauto-block-<port>`) and one literal nft
# table (`badauto`), so blue grepped the name once and cleared every block red
# had ever placed. Here each activation draws its own names — which is also what
# makes the undo exact: the recipe names THIS activation's table/rule, so a
# second block on the same box cannot collide with (or inherit) the first.

BLOCK_ROLES_LINUX = ("table", "chain")
BLOCK_ROLES_WINDOWS = ("rule",)


def _nft_safe(word):
    """nft identifiers are [A-Za-z0-9_]: a hyphenated stem must be folded, or
    `nft add table inet dbus-broker-refresh` is a syntax error."""
    return re.sub(r"[^A-Za-z0-9_]", "_", word or "")


def block_names(world, rng=None, mode=MODE_FULL, windows=False):
    """The names one port block wears: the nft table + hook chain (Linux) or the
    Windows firewall rule name. Drawn per activation from the OS pool."""
    rng = rng or random
    if windows:
        labels = _labels_for(world, WIN_STEMS, len(BLOCK_ROLES_WINDOWS), mode, rng)
        return {"rule": title(labels[0]).replace("_", ""), "mode": mode}
    labels = _labels_for(world, LINUX_STEMS, len(BLOCK_ROLES_LINUX), mode, rng)
    table, chain = labels
    return {"table": _nft_safe(table), "chain": _nft_safe(chain), "mode": mode}
