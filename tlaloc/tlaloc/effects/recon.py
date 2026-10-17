"""recon — service/version scan of one team box, run locally on red01.

Read-only, so it is legal in the ladder's `observe` tier (the only thing that
is), and it is the effect the loop falls back to when nothing else is bounded
yet. The result merges into world targets, which is what every consumer of
`world.target(ip)["ports"]` reads.
"""

import shutil
import xml.etree.ElementTree as ET

from ..errors import PolicyError
from .base import Effect, Result, register

DEFAULT_TOP_PORTS = 200
DEFAULT_TIMING = "T4"


def parse_ports(xml_text):
    """nmap XML -> [{port, proto, state, service, version}]."""
    ports = []
    for host in ET.fromstring(xml_text).iter("host"):
        for p in host.iter("port"):
            svc = p.find("service")
            state = p.find("state")
            ports.append({
                "port": int(p.get("portid")),
                "proto": p.get("protocol"),
                "state": state.get("state") if state is not None else "?",
                "service": svc.get("name") if svc is not None else None,
                "version": (f"{svc.get('product', '')} {svc.get('version', '')}".strip()
                            if svc is not None else None),
            })
    return ports


@register
class Recon(Effect):
    id = "recon"
    name = "nmap service scan"
    description = ("Scan one team box for open ports and service versions (runs on "
                   "red01). Read-only: allowed during the observe tier and the "
                   "safe pick when every impact effect is still ladder-locked.")
    params = {
        "ip": {"type": "str", "required": True, "desc": "target box"},
        "ports": {"type": "str", "desc": "explicit nmap port spec (else top-ports)"},
    }
    tags = frozenset({"access", "passive"})
    needs = {"foothold": False, "root": False, "c2": False, "os": "any"}

    def check(self, ctx, params):
        super().check(ctx, params)
        policy = getattr(ctx, "policy", None)
        if policy is not None and not policy.recon_allowed():
            raise PolicyError("recon cutoff reached — spend the remaining window "
                              "on access and pressure")

    def run(self, ctx, params):
        ip = params["ip"]
        if shutil.which("nmap") is None:
            return Result(False, "nmap is not installed on red01")

        limits = ctx.cfg["limits"]
        spec = (params.get("ports")
                or f"--top-ports {limits.get('nmap_top_ports', DEFAULT_TOP_PORTS)}")
        timing = limits.get("nmap_timing", DEFAULT_TIMING)
        cmd = f"nmap -sV --open -Pn -{timing} {spec} -oX - {ip}"
        timeout = int(limits.get("scan_timeout", 180))
        policy = getattr(ctx, "policy", None)
        if policy is not None:
            timeout = max(min(timeout, int(policy.remaining_sec()) or timeout), 5)

        rc, out, err = ctx.ssh.run_local(cmd, timeout=timeout, dry_run=ctx.dry_run)
        if rc != 0 or not (out or "").strip():
            return Result(False, f"nmap rc={rc}: {(err or '').strip()[:200]}")
        try:
            ports = parse_ports(out)
        except ET.ParseError as exc:
            return Result(False, f"nmap XML parse failed: {exc}")

        open_ports = [p for p in ports if p["state"] == "open"]
        ctx.world.note_recon(ip, open_ports, os_guess=guess_os(open_ports))
        kinds = port_kinds(open_ports)
        if kinds:
            ctx.world.note(ip, f"recon: {len(open_ports)} open ports, "
                               f"kinds={sorted(kinds)}")
        return Result(True, f"{len(open_ports)} open ports on {ip}",
                      data={"ports": open_ports})


def port_kinds(open_ports):
    kinds = set()
    for p in open_ports:
        if p.get("service") in ("http", "https") or p.get("port") in (80, 443, 8080):
            kinds.add("web")
        if p.get("service") == "ssh" or p.get("port") == 22:
            kinds.add("ssh")
        if p.get("service") in ("microsoft-ds", "netbios-ssn") or p.get("port") in (445, 139):
            kinds.add("smb")
        if p.get("service") in ("ms-wbt-server",) or p.get("port") == 3389:
            kinds.add("rdp")
        if p.get("service") in ("ldap", "ldaps") or p.get("port") in (389, 636):
            kinds.add("ldap")
    return kinds


def guess_os(open_ports):
    """A weak hint only: the authoritative OS comes from intel/boxes.json."""
    for p in open_ports:
        if p.get("service") in ("ms-wbt-server", "microsoft-ds", "netbios-ssn"):
            return "windows"
    return None
