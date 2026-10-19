"""block_port — drop a scored check port with a local firewall rule.

The most CCDC-authentic failure mode there is: the daemon keeps running, the
port is unreachable, the check fails, and blue has to find the rule. The pin in
`intel.port_checks_for()` is a *bare port check*, so it has no unit to stop —
this is the only mechanism that touches it.

One effect, three Linux mechanisms in preference order, all attempted in one
script so the box itself says which one landed:

* ``iptables`` ``-I INPUT -p tcp --dport <port> -j REJECT`` (the earlier agent's
  mechanic)
* ``ufw``      ``ufw insert 1 deny <port>/tcp`` (a different backend: on
                firewalld hosts the iptables call can be a shim that says yes)
* ``nft``      an ``inet`` table of OUR OWN, hooked at ``input`` — the lesson
                the earlier agent learned after ``nft insert rule inet filter
                input`` died with "No such file or directory" on a firewalld box
                (caught live, three failed actions)

Each mechanism echoes its own return code, so the box says which one landed
(the earlier agent's `_picked_cleanup` fix, kept). What tlaloc adds is that an
add's own rc is never taken as proof: a second pass reads every rule BACK, and
only a rule that holds becomes an impact — the ledger is what scoreboard
attribution is graded on. The restore recipe then names the mechanism(s) that
hold and this activation's drawn table/rule name (never a fixed one blue can
grep once and clear forever).

Two bugs from the earlier agent are fixed in the mechanics themselves rather than
copied:

* the nft brace spec is single-quoted. Unquoted, the ``;`` inside the braces
  ends the shell command and the trailing ``}`` is a syntax error — the earlier
  agent's nft fallback could not have worked on any box;
* the whole script runs under ONE root shell (``sh -c``, the earlier agent's own
  root_cmd idiom). A bare ``sudo -n`` prefix binds to the first command only, so
  ``sudo -n iptables …; echo …; ufw …`` would run everything after the first
  ``;`` as the foothold user and silently half-apply.

Windows is the Administrator path: a ``netsh advfirewall`` block rule (present
on every SKU, unlike New-NetFirewallRule's module), verified by reading the rule
back.
"""

import re
import shlex

from ..errors import PolicyError
from . import artifactnames as names_mod
from .base import Effect, Result, can_root, register, root_cmd
from .takedown import windows_admin

# (mechanism, marker) in preference order: the first that HOLDS is what we claim.
LINUX_MECHANISMS = (("iptables", "i"), ("ufw", "u"), ("nft", "n"))
WINDOWS_MARKER = "TLALOC_BLOCK"

# Blocking our own access path is a self-lockout, exactly like stopping sshd.
OWN_ACCESS_PORTS = (22,)

_MARKERS = re.compile(r"([a-z]+)=(\d+)")


def run_root_script(ctx, ip, fh, script, timeout=60):
    """Run ONE multi-command script under ONE root shell.

    `root_cmd` only *prefixes* the escalation label, so a bare prefix covers the
    first command of a `;`-separated script and nothing after it
    (`sudo -n iptables -I …; echo …; ufw …` runs the ufw half unprivileged).
    the earlier agent's root_cmd wrapped multi-command scripts in `sh -c`; this
    is that idiom, kept in ONE place so the three pressure effects cannot drift
    apart.
    """
    return ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                       root_cmd(f"sh -c {shlex.quote(script)}", fh),
                       timeout=timeout, key_path=fh.get("key_path"))


def add_script(port, table, chain):
    """All three Linux mechanisms, each echoing its own rc."""
    return (
        f"iptables -I INPUT -p tcp --dport {port} -j REJECT 2>/dev/null; "
        f"echo i=$?; "
        f"ufw insert 1 deny {port}/tcp 2>/dev/null; echo u=$?; "
        f"nft add table inet {table} 2>/dev/null; "
        f"nft 'add chain inet {table} {chain} "
        f"{{ type filter hook input priority -10 ; }}' 2>/dev/null; "
        f"nft add rule inet {table} {chain} tcp dport {port} reject "
        f"2>/dev/null; echo n=$?"
    )


def verify_script(port, table, chain):
    """Which rule actually HOLDS. The nft check reads the chain back, so a rule
    that landed in an unhooked chain is not counted as a block."""
    return (
        f"iptables -C INPUT -p tcp --dport {port} -j REJECT >/dev/null 2>&1 "
        f"&& echo vi=1 || echo vi=0; "
        f"ufw status 2>/dev/null | grep -qE '^{port}/tcp[[:space:]]+(DENY|REJECT)' "
        f"&& echo vu=1 || echo vu=0; "
        f"if nft list chain inet {table} {chain} 2>/dev/null | grep -q 'hook input' "
        f"&& nft list chain inet {table} {chain} 2>/dev/null | grep -q 'dport {port}'; "
        f"then echo vn=1; else echo vn=0; fi"
    )


def undo_command(mechanism, port, table):
    if mechanism == "iptables":
        return f"iptables -D INPUT -p tcp --dport {port} -j REJECT"
    if mechanism == "ufw":
        return f"ufw delete deny {port}/tcp"
    return f"nft delete table inet {table}"


def windows_add(port, rule):
    """The earlier agent's Windows mechanic: netsh exists on every SKU under an
    admin shell."""
    return (f"netsh advfirewall firewall add rule name={rule} dir=in "
            f"action=block protocol=TCP localport={port}")


def windows_verify(port, rule):
    """Read the rule back; `show rule` prints the LocalPort and the Action."""
    return (f"$o = netsh advfirewall firewall show rule name={rule} 2>$null; "
            f"if ($o -match '{rule}' -and $o -match 'Block' -and $o -match '{port}') "
            f"{{ '{WINDOWS_MARKER}=ok' }} else {{ '{WINDOWS_MARKER}=no' }}")


def windows_undo(rule):
    return f"netsh advfirewall firewall delete rule name={rule}"


# No `check_cmd` on these artifacts, deliberately. The loop's liveness check runs
# a plain command as the foothold user, and there is no unprivileged,
# mechanism-independent way to read a firewall rule back (iptables/ufw/nft all
# need root, and `netsh show` needs elevation). A check that fails for the wrong
# reason marks the artifact DEAD — and policy treats any dead artifact on a box
# as "re-plant first, this box cannot be pressured", which would silently switch
# off every takedown candidate there. A record that cannot lie is worth more than
# one that can: the restore table and the impact ledger carry the block, and the
# detail names the exact rule and its unblock command.
def _artifact(ip, detail, paths=None):
    return {"ip": ip, "kind": "impact-block_port", "detail": detail,
            "check_cmd": None, "paths": list(paths or [])}


@register
class BlockPort(Effect):
    id = "block_port"
    name = "firewall-drop a scored port"
    description = (
        "Insert a local firewall block for a scored port check so the scoring "
        "probe fails while the daemon stays up — use it for a pin that is a bare "
        "port check (intel's score_only pins), where a takedown has no unit to "
        "stop. Linux (root): iptables REJECT, else ufw, else an nft table of our "
        "own; Windows (Administrator): a netsh advfirewall block rule. The "
        "mechanism that actually holds is verified on the box before the impact "
        "is claimed, and the restore recipe names it and its drawn rule name. "
        "Refuses port 22 (our own path) unless allow_ssh=true.")
    params = {
        "ip": {"type": "str", "required": True, "desc": "target box"},
        "port": {"type": "int", "required": True,
                 "desc": "scored TCP check port to drop"},
        "display": {"type": "str",
                    "desc": "the scoreboard's name for the pin (from intel)"},
        "allow_ssh": {"type": "str",
                      "desc": "'true' to allow blocking port 22 — off by default, "
                              "because that is our own access path"},
    }
    tags = frozenset({"pressure", "impacty"})
    needs = {"foothold": True, "root": False, "c2": False, "os": "any"}
    failure_scope = "port"

    # -- preconditions ---------------------------------------------------------

    def check(self, ctx, params):
        super().check(ctx, params)
        ip = params["ip"]
        port = int(params["port"])
        if not 1 <= port <= 65535:
            raise PolicyError(f"block_port: {port} is not a TCP port")
        fh = ctx.world.foothold(ip)
        if fh.get("windows"):
            if not windows_admin(fh):
                raise PolicyError(f"block_port needs an Administrator foothold "
                                  f"on {ip}")
        elif not can_root(fh):
            raise PolicyError(f"block_port needs a root path on {ip} — escalate first")
        if port in OWN_ACCESS_PORTS and not self._allow_ssh(params):
            raise PolicyError("refusing to block port 22 — that is our own access "
                              "path (allow_ssh=true to override)")
        policy = getattr(ctx, "policy", None)
        if policy is not None:
            if not policy.unrecoverable_unlocked():
                raise PolicyError(
                    "block_port is an unrecoverable move — locked until the second "
                    "half or the tier that unlocks it")
            ok, why = policy.can_impact(ctx.intel.team_of(ip))
            if not ok:
                raise PolicyError(why)
        if ctx.world.failure_count(self.id, ip, port) >= 2:
            raise PolicyError(f"block_port already failed twice on {ip}:{port} — "
                              "inspect the box instead of re-running")

    @staticmethod
    def _allow_ssh(params):
        return str(params.get("allow_ssh", "")).lower() == "true"

    # -- the body --------------------------------------------------------------

    def run(self, ctx, params):
        ip, port = params["ip"], int(params["port"])
        display = params.get("display") or f"tcp/{port}"
        fh = ctx.world.foothold(ip)
        if fh is None:
            return Result(False, f"no foothold on {ip}")
        if port in OWN_ACCESS_PORTS and not self._allow_ssh(params):
            return Result(False, "refusing to block port 22 — that is our own "
                                 "access path (allow_ssh=true to override)")
        mode = names_mod.mode_from_cfg(ctx.cfg)
        if fh.get("windows"):
            return self._windows(ctx, ip, fh, port, display, mode)

        if not can_root(fh):
            return Result(False, f"no root path on {ip} — privesc first")
        names = names_mod.block_names(ctx.world, rng=ctx.rng, mode=mode)
        table, chain = names["table"], names["chain"]

        rc, out, err = run_root_script(ctx, ip, fh, add_script(port, table, chain))
        added = _MARKERS.findall(out or "")
        inserted = [name for name, marker in LINUX_MECHANISMS
                    if dict(added).get(marker) == "0"]
        if not inserted:
            return Result(False, f"no firewall mechanism accepted the rule on {ip} "
                                 f"(rc={rc}): {(err or out or '').strip()[:200]}")

        rc, out, err = run_root_script(ctx, ip, fh,
                                       verify_script(port, table, chain), timeout=45)
        seen = dict(_MARKERS.findall(out or ""))
        holding = [name for name, marker in LINUX_MECHANISMS
                   if seen.get(f"v{marker}") == "1"]
        if not holding:
            # The objective is NOT met, so this is ok=False — but the rule the box
            # accepted is real, so its undo is recorded (installed=True). An
            # unnecessary `iptables -D` costs nothing; a missing one leaves blue's
            # traffic blocked after the event.
            detail = (f"tcp/{port} block did not hold on {ip} — a rule was inserted "
                      f"({inserted}) but no mechanism reports it: "
                      f"{(err or out or '').strip()[:200]}")
            recipe = [f"# block_port on {ip}: a rule was accepted but not verified — "
                      "undo it to be safe"]
            recipe += [undo_command(m, port, table) for m in inserted]
            return Result(False, detail, restore={"recipe": recipe, "secrets": None},
                          artifacts=[_artifact(
                              ip, f"tcp/{port} ({display}) inserted via {inserted} on "
                                  f"{ip} but NOT verified as blocking")],
                          installed=True)

        mechanism = holding[0]
        detail = (f"tcp/{port} ({display}) blocked on {ip} via {mechanism}")
        if mechanism == "nft":
            detail += f" (table inet {table})"
        ctx.world.note(ip, f"impact: {detail}")
        recipe = [f"# block_port on {ip}: undo the rule(s) this activation added"]
        recipe += [undo_command(m, port, table) for m in holding]
        recipe += [f"# {m}: inserted but NOT enforcing — undo it too: "
                   f"{undo_command(m, port, table)}"
                   for m in inserted if m not in holding]
        return Result(True, detail,
                      impact={"ip": ip, "service": display, "mode": "block_port",
                              "detail": detail},
                      restore={"recipe": recipe, "secrets": None},
                      artifacts=[_artifact(
                          ip, f"{detail}; unblock: "
                              f"{undo_command(mechanism, port, table)}")])

    def _windows(self, ctx, ip, fh, port, display, mode):
        if not windows_admin(fh):
            return Result(False, f"block_port needs an Administrator foothold on {ip}")
        rule = names_mod.block_names(ctx.world, rng=ctx.rng, mode=mode,
                                     windows=True)["rule"]
        rc, out, err = ctx.ssh.ssh_ps(ip, fh["user"], fh.get("password") or "",
                                      windows_add(port, rule), timeout=60,
                                      key_path=fh.get("key_path"))
        if rc != 0:
            return Result(False, f"netsh rc={rc}: {(err or out or '').strip()[:200]}")
        rc, out, err = ctx.ssh.ssh_ps(ip, fh["user"], fh.get("password") or "",
                                      windows_verify(port, rule), timeout=45,
                                      key_path=fh.get("key_path"))
        if f"{WINDOWS_MARKER}=ok" not in (out or ""):
            # Not verified: ok=False, but the rule the box accepted still gets its
            # undo recorded (installed=True).
            detail = (f"tcp/{port} block did not hold on {ip} — rule {rule} is not "
                      f"reporting Block: {(err or out or '').strip()[:200]}")
            return Result(False, detail,
                          restore={"recipe": [
                              f"# block_port on {ip}: the rule was accepted but not "
                              "verified — delete it to be safe",
                              windows_undo(rule)], "secrets": None},
                          artifacts=[_artifact(
                              ip, f"tcp/{port} ({display}) inserted as rule {rule} on "
                                  f"{ip} but NOT verified as blocking")],
                          installed=True)
        detail = f"tcp/{port} ({display}) blocked on {ip} via netsh rule {rule}"
        ctx.world.note(ip, f"impact: {detail}")
        # The on-box check is `netsh show` (needs an elevated session, which is
        # the same session the block needed).
        return Result(True, detail,
                      impact={"ip": ip, "service": display, "mode": "block_port",
                              "detail": detail},
                      restore={"recipe": [
                          f"# block_port on {ip}: undo the rule this activation added",
                          windows_undo(rule)],
                          "secrets": None},
                      artifacts=[_artifact(
                          ip, f"{detail}; unblock: {windows_undo(rule)}")])
