"""Jump VM firewall/sysctl content: pure rule generation (golden-filed in tests)."""

from ipaddress import ip_network
from estate import config

JUMP_TAGS_EXTRA = "jump"
APT_CACHER_PORT = 3142


def jump_rules(team_identifiers, engine_mgmt_ip, mgmt_cidr="10.0.0.0/24",
               red_segment=""):
    # Without red_segment's accepts + SNAT, red's source falls off the FORWARD DROP.
    # TCPMSS clamps stop a sub-1500 inter-node path black-holing large replies.
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
        # Public-LLM egress: no operator-side tunnel reaches red's cloud endpoint, so red dials it like the teams.
        lines.append(f"-A FORWARD -s {red} ! -d 192.168.0.0/16 -j ACCEPT")
    for t in teams:
        lines.append(f"-A FORWARD -s 192.168.{t}.0/24 -d {engine} -j ACCEPT")
    if red:
        # The jump is the teams' default gateway: unless this accept matches FIRST, replies to
        # red leave via the mgmt default route.
        for t in teams:
            lines.append(f"-A FORWARD -s 192.168.{t}.0/24 -d {red} -j ACCEPT")
    for t in teams:
        lines.append(f"-A FORWARD -s 192.168.{t}.0/24 ! -d 192.168.0.0/16 -j ACCEPT")
    lines += ["COMMIT", "*nat"]
    for t in teams:
        lines.append(f"-A PREROUTING -d 192.168.{t}.1/32 -p tcp --dport {APT_CACHER_PORT} "
                     f"-j DNAT --to-destination {engine_mgmt_ip}:{APT_CACHER_PORT}")
    for t in teams:
        # Boxes trust SSH only from 192.168.<id>.1, so engine->box traffic SNATs to the gateway IP.
        lines.append(f"-A POSTROUTING -s {engine} -d 192.168.{t}.0/24 "
                     f"-j SNAT --to-source 192.168.{t}.1")
    if red:
        for t in teams:
            # Must PRECEDE the team-egress MASQUERADE below (first match wins in nat
            # POSTROUTING) or red's source is rewritten twice.
            lines.append(f"-A POSTROUTING -s {red} -d 192.168.{t}.0/24 "
                         f"-j SNAT --to-source 192.168.{t}.1")
        # Red's WAN egress: AFTER the per-team SNAT rules, so team-bound red traffic keeps its gateway source.
        lines.append(f"-A POSTROUTING -s {red} ! -d 192.168.0.0/16 -j MASQUERADE")
    for t in teams:
        # Terminal ACCEPT leaves engine-bound traffic unmangled (first match wins);
        # iptables-restore rejects two -d in one rule, so the exclusion can't ride it.
        lines.append(f"-A POSTROUTING -s 192.168.{t}.0/24 -d {engine} -j ACCEPT")
        lines.append(f"-A POSTROUTING -s 192.168.{t}.0/24 ! -d 192.168.0.0/16 -j MASQUERADE")
    lines.append("COMMIT")
    return "\n".join(lines) + "\n"


def red_segment_from_env():
    # One segment, one definition (common.constants): a second knob let the engine and
    # the satellite jumps disagree about the address red01 actually lives on.
    from common.constants import RED_SEGMENT_SUBNET
    return str(ip_network(RED_SEGMENT_SUBNET, strict=False))


def jump_sysctl_script():
    return ("printf 'net.ipv4.ip_forward=1\\nnet.ipv4.conf.all.rp_filter=2\\n"
            "net.ipv4.conf.default.rp_filter=2\\n' | sudo tee /etc/sysctl.d/99-jump.conf; "
            "sudo sysctl -p /etc/sysctl.d/99-jump.conf")
