"""block_port — drop a scored check port with a local firewall rule; only a rule read back from the box becomes an impact."""

import re
import shlex

from ..errors import PolicyError
from . import artifactnames as names_mod
from .base import Effect, Result, can_root, register, root_cmd
from .takedown import windows_admin

# (mechanism, marker) preference order: the first that HOLDS is what we claim.
LINUX_MECHANISMS = (("iptables", "i"), ("ufw", "u"), ("nft", "n"))
WINDOWS_MARKER = "TLALOC_BLOCK"

# Blocking our own access path is a self-lockout, exactly like stopping sshd.
OWN_ACCESS_PORTS = (22,)

_MARKERS = re.compile(r"([a-z]+)=(\d+)")


def run_root_script(ctx, ip, fh, script, timeout=60):
    """Multi-command scripts need ONE root shell: `root_cmd` only *prefixes*, so a bare
    escalation label covers the first command of a `;`-list and nothing after it."""
    return ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                       root_cmd(f"sh -c {shlex.quote(script)}", fh),
                       timeout=timeout, key_path=fh.get("key_path"))


# The nft brace spec must stay quoted: unquoted, the `;` inside the braces ends the
# shell command and the trailing `}` is a syntax error.
def add_script(port, table, chain):
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
    """The nft check requires the hook, so a rule in an unhooked chain is not a block."""
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
    """Chosen over New-NetFirewallRule: netsh exists on every SKU under an admin shell."""
    return (f"netsh advfirewall firewall add rule name={rule} dir=in "
            f"action=block protocol=TCP localport={port}")


def windows_verify(port, rule):
    """`netsh show rule` prints the LocalPort and the Action."""
    return (f"$o = netsh advfirewall firewall show rule name={rule} 2>$null; "
            f"if ($o -match '{rule}' -and $o -match 'Block' -and $o -match '{port}') "
            f"{{ '{WINDOWS_MARKER}=ok' }} else {{ '{WINDOWS_MARKER}=no' }}")


def windows_undo(rule):
    return f"netsh advfirewall firewall delete rule name={rule}"


# No check_cmd: reading a rule back needs root (or elevation), and a check failing for
# the wrong reason marks the artifact DEAD — which switches off every pressure candidate
# on the box. The restore table and impact ledger carry the block instead.
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
            # Objective not met -> ok=False, but the accepted rule is real: its undo is
            # recorded (installed=True). A missing `-D` leaves blue blocked after the event.
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
            # Not verified: ok=False, but the accepted rule still gets its undo recorded.
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
        return Result(True, detail,
                      impact={"ip": ip, "service": display, "mode": "block_port",
                              "detail": detail},
                      restore={"recipe": [
                          f"# block_port on {ip}: undo the rule this activation added",
                          windows_undo(rule)],
                          "secrets": None},
                      artifacts=[_artifact(
                          ip, f"{detail}; unblock: {windows_undo(rule)}")])
