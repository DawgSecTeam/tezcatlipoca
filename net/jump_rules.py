"""Jump VM firewall/sysctl content: pure rule generation (golden-filed in tests)."""

from ipaddress import ip_network
from estate import config

JUMP_TAGS_EXTRA = "jump"
APT_CACHER_PORT = 3142


def jump_rules(team_identifiers, engine_mgmt_ip, mgmt_cidr="10.0.0.0/24",
               red_segment=""):
    # iptables-restore content for the jump; FORWARD DROP with explicit accepts, so team-to-team
    # and mgmt->team isolation is structural. `red_segment` adds the routed-red path (FORWARD
    # accept + SNAT to the team gateway) without which red's source matches nothing and falls off
    # the DROP. The TCPMSS clamps stop a sub-1500 inter-node path black-holing large replies.
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
        # Public-LLM egress: red's cloud endpoint has no operator-side tunnel able to carry it, so
        # red dials it directly — the same egress shape the teams have.
        lines.append(f"-A FORWARD -s {red} ! -d 192.168.0.0/16 -j ACCEPT")
    for t in teams:
        lines.append(f"-A FORWARD -s 192.168.{t}.0/24 -d {engine} -j ACCEPT")
    if red:
        # Return path: the jump is the teams' default gateway, so a reply to red leaves via the
        # mgmt default route unless this accept matches FIRST (the engine -d rule covers only itself).
        for t in teams:
            lines.append(f"-A FORWARD -s 192.168.{t}.0/24 -d {red} -j ACCEPT")
    for t in teams:
        lines.append(f"-A FORWARD -s 192.168.{t}.0/24 ! -d 192.168.0.0/16 -j ACCEPT")
    lines += ["COMMIT", "*nat"]
    for t in teams:
        lines.append(f"-A PREROUTING -d 192.168.{t}.1/32 -p tcp --dport {APT_CACHER_PORT} "
                     f"-j DNAT --to-destination {engine_mgmt_ip}:{APT_CACHER_PORT}")
    for t in teams:
        # Engine -> box traffic sources from the gateway IP: boxes trust SSH only from
        # 192.168.<id>.1.
        lines.append(f"-A POSTROUTING -s {engine} -d 192.168.{t}.0/24 "
                     f"-j SNAT --to-source 192.168.{t}.1")
    if red:
        for t in teams:
            # Same gateway-IP treatment for red; it must PRECEDE the team-egress MASQUERADE
            # below (first match wins in nat POSTROUTING) or red's source is rewritten twice.
            lines.append(f"-A POSTROUTING -s {red} -d 192.168.{t}.0/24 "
                         f"-j SNAT --to-source 192.168.{t}.1")
        # Red's WAN egress: AFTER the per-team SNAT rules, so team-bound red traffic keeps its gateway source.
        lines.append(f"-A POSTROUTING -s {red} ! -d 192.168.0.0/16 -j MASQUERADE")
    for t in teams:
        # Engine-bound traffic is left UNmangled via a terminal ACCEPT (first match wins in nat
        # POSTROUTING); iptables-restore rejects two -d in one rule, so the exclusion can't ride it.
        lines.append(f"-A POSTROUTING -s 192.168.{t}.0/24 -d {engine} -j ACCEPT")
        lines.append(f"-A POSTROUTING -s 192.168.{t}.0/24 ! -d 192.168.0.0/16 -j MASQUERADE")
    lines.append("COMMIT")
    return "\n".join(lines) + "\n"


def red_segment_from_env():
    # `TEZ_RED_SEGMENT` as a CIDR, or "" (no red rules — single-node and masq-mode red unchanged).
    # A malformed value is a hard error: a typo that silently produced no rules would put a live
    # range back in the state that lost the soak's red coverage.
    raw = (config.active().knobs.red_segment or "").strip()
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
