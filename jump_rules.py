"""Jump VM firewall/sysctl content: pure rule generation (golden-filed in tests)."""

import os
from ipaddress import ip_network

JUMP_TAGS_EXTRA = "jump"
APT_CACHER_PORT = 3142


def jump_rules(team_identifiers, engine_mgmt_ip, mgmt_cidr="10.0.0.0/24",
               red_segment=""):
    """iptables-restore content for the jump. Pure — golden-filed in tests.

    FORWARD defaults to DROP; accepts are exactly: established/related, engine ->
    team subnets (scoring/nakon/verify), team -> engine (apt/DNAT callbacks), and
    team -> anything outside 192.168.0.0/16 (internet + mgmt LAN — the same egress
    the engine's MASQUERADE grants local teams). Team -> team and mgmt -> team fall
    off the default DROP: structural isolation.

    `red_segment` (a CIDR, or "" for none) adds the routed-red path through the
    satellite hop: FORWARD accept red -> local teams, and SNAT to the team gateway
    so the boxes' gateway-IP-only SSH trust still sees 192.168.<id>.1 — the same
    treatment engine -> box traffic gets. Without it red's source matches nothing
    and falls off the FORWARD DROP: scale8-soak-2026-10-02 measured 15/15
    cred_sprays failing "unreachable over SSH" against satellite teams for exactly
    that reason. Return traffic needs no rule: conntrack de-SNATs it and the
    established/related accept above carries it. Empty (the default) reproduces the
    pre-soak ruleset byte for byte, so single-node and red-less comps are unchanged.

    The TCPMSS clamps are not red-specific: this VM is the first routed hop between
    two /24s, and a path-MTU below 1500 on the inter-node link black-holes large
    replies unless the hop clamps them. `--clamp-mss-to-pmtu` is a no-op when both
    sides agree on the MTU."""
    teams = [str(i) for i in team_identifiers]
    engine = f"{engine_mgmt_ip}/32"
    mgmt_prefix = ip_network(mgmt_cidr, strict=False).network_address
    red = f"{ip_network(red_segment, strict=False)}" if red_segment else ""
    lines = ["*filter",
             ":INPUT ACCEPT",
             ":FORWARD DROP",
             ":OUTPUT ACCEPT",
             "-A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"]
    for zone in (f"{mgmt_prefix}/24", red):
        if not zone:
            continue
        for t in teams:
            lines.append(f"-A FORWARD -s {zone} -d 192.168.{t}.0/24 "
                         f"-p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu")
    for t in teams:
        lines.append(f"-A FORWARD -s {engine} -d 192.168.{t}.0/24 -j ACCEPT")
    if red:
        for t in teams:
            lines.append(f"-A FORWARD -s {red} -d 192.168.{t}.0/24 -j ACCEPT")
    for t in teams:
        lines.append(f"-A FORWARD -s 192.168.{t}.0/24 -d {engine} -j ACCEPT")
    if red:
        # Return path: the jump is the teams' default gateway (192.168.<id>.1), so a
        # reply to red leaves via the mgmt default route unless this accept matches
        # FIRST. The engine -d rule above only covers the engine's own address.
        for t in teams:
            lines.append(f"-A FORWARD -s 192.168.{t}.0/24 -d {red} -j ACCEPT")
    for t in teams:
        lines.append(f"-A FORWARD -s 192.168.{t}.0/24 ! -d 192.168.0.0/16 -j ACCEPT")
    lines += ["COMMIT", "*nat"]
    for t in teams:
        lines.append(f"-A PREROUTING -d 192.168.{t}.1/32 -p tcp --dport {APT_CACHER_PORT} "
                     f"-j DNAT --to-destination {engine_mgmt_ip}:{APT_CACHER_PORT}")
    for t in teams:
        # Engine -> box traffic sources from the gateway IP: boxes trust SSH only
        # from 192.168.<id>.1 (gateway-IP auth preserved across the routed hop).
        lines.append(f"-A POSTROUTING -s {engine} -d 192.168.{t}.0/24 "
                     f"-j SNAT --to-source 192.168.{t}.1")
    if red:
        for t in teams:
            # Same gateway-IP treatment for red, and it must PRECEDE the team-egress
            # MASQUERADE below (first match wins in nat POSTROUTING) or red's source
            # would be rewritten twice and arrive as the jump's mgmt address.
            lines.append(f"-A POSTROUTING -s {red} -d 192.168.{t}.0/24 "
                         f"-j SNAT --to-source 192.168.{t}.1")
    for t in teams:
        # Engine-bound traffic is left UNmangled via a terminal ACCEPT (first match
        # wins in nat POSTROUTING) — the engine sees real box sources, same as with
        # local teams today. iptables-restore rejects two -d in one rule, so the
        # exclusion can't ride the MASQUERADE line itself.
        lines.append(f"-A POSTROUTING -s 192.168.{t}.0/24 -d {engine} -j ACCEPT")
        lines.append(f"-A POSTROUTING -s 192.168.{t}.0/24 ! -d 192.168.0.0/16 -j MASQUERADE")
    lines.append("COMMIT")
    return "\n".join(lines) + "\n"


def red_segment_from_env():
    """`TEZ_RED_SEGMENT` as a CIDR, or "" when red has no separate segment.

    Empty is the safe default: no red rules are emitted, so single-node ranges and
    masq-mode red behave exactly as before. A malformed value is a hard error — a typo
    that silently produced no rules would put a live range back in the state that lost
    the whole soak's red coverage."""
    raw = (os.environ.get("TEZ_RED_SEGMENT") or "").strip()
    if not raw:
        return ""
    try:
        return str(ip_network(raw, strict=False))
    except ValueError as e:
        raise SystemExit(f"  ERROR: TEZ_RED_SEGMENT={raw!r} is not a CIDR: {e}")


def jump_sysctl_script():
    return ("printf 'net.ipv4.ip_forward=1\\nnet.ipv4.conf.all.rp_filter=2\\n"
            "net.ipv4.conf.default.rp_filter=2\\n' | sudo tee /etc/sysctl.d/99-jump.conf; "
            "sudo sysctl -p /etc/sysctl.d/99-jump.conf")
