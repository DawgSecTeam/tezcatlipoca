"""In-path firewall plumbing: per-team config generation, SSH bootstrap, and the engine cutover that moves the team gateway onto the firewall."""

import base64
import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import termios
import time

from infra.guest_exec import guest_agent_exec_root
from infra.client import proxmox_api
from common.ssh_ops import gateway_proxy, ssh_on_gateway, ssh_via_gateway
from estate import config

# (port, label) of engine services boxes reach at the team gateway address (apt's proxy uses 3142).
ENGINE_GATEWAY_SERVICES = ((3142, "apt-cacher"),)
# The provisioning template's LAN address; the engine borrows the one beside it.
BOOTSTRAP_FW_IP = "192.168.1.1"
BOOTSTRAP_ENGINE_IP = "192.168.1.2"
FW_USER = "admin"            # uid 0 on pfSense; the deploy key is in its authorizedkeys
FW_USER_VYOS = "vyos"        # config mode works as the plain vyos user
# pfSense boot never runs /usr/local/etc/rc.d/*, so the rc.conf enable is inert; this <system><afterbootupshellcmd> tag (mwexec'd by /etc/rc.bootup) is the pfSense-native hook.
QGA_START_SHELLCMD = "/usr/local/etc/rc.d/qemu-guest-agent start"
FW_BOOT_BUDGET_S = 420       # a cloned appliance's first boot to SSH
FW_APPLY_BUDGET_S = 600      # config push → reboot → SSH on the WAN address (a loaded node boots slowly)
FW_PUSH_ATTEMPT_S = 30       # one push attempt; sshd answers before a booting appliance can log in


# The WAN pass rule must be the FIRST <rule> and its destination the `lan` KEYWORD — pfSense's
# <network> field takes a keyword/alias, not a CIDR, and a raw CIDR silently drops the rule.
def generate_team_config(seed_xml, team_id, red_dnat_spec=None, authorized_key=None):
    tid = str(team_id)
    x = seed_xml

    x = x.replace("<ssh></ssh>", "<ssh><enable>enabled</enable></ssh>")
    if "afterbootupshellcmd" not in x:
        x = x.replace("<system>",
                      f"<system><afterbootupshellcmd>{QGA_START_SHELLCMD}</afterbootupshellcmd>", 1)
    if authorized_key:
        b64 = base64.b64encode(authorized_key.strip().encode() + b"\n").decode()
        x = x.replace("<name>admin</name>",
                      f"<name>admin</name><authorizedkeys>{b64}</authorizedkeys>", 1)
    x = x.replace("<hostname>pfSense</hostname>", f"<hostname>fw-team{tid}</hostname>")

    new_ifaces = f"""<interfaces>
\t\t<wan>
\t\t\t<enable></enable>
\t\t\t<if>vtnet0</if>
\t\t\t<ipaddr>172.31.{tid}.2</ipaddr>
\t\t\t<subnet>30</subnet>
\t\t\t<gateway>WANGW</gateway>
\t\t\t<ipaddrv6></ipaddrv6>
\t\t\t<subnetv6></subnetv6>
\t\t</wan>
\t\t<lan>
\t\t\t<enable></enable>
\t\t\t<if>vtnet1</if>
\t\t\t<ipaddr>192.168.{tid}.1</ipaddr>
\t\t\t<subnet>24</subnet>
\t\t\t<ipaddrv6></ipaddrv6>
\t\t\t<subnetv6></subnetv6>
\t\t</lan>
\t</interfaces>"""
    i0 = x.index("<interfaces>")
    i1 = x.index("</interfaces>") + len("</interfaces>")
    x = x[:i0] + new_ifaces + x[i1:]

    x = x.replace("<gateways></gateways>", f"""<gateways>
\t\t<gateway_item>
\t\t\t<interface>wan</interface>
\t\t\t<gateway>172.31.{tid}.1</gateway>
\t\t\t<name>WANGW</name>
\t\t\t<weight>1</weight>
\t\t\t<ipprotocol>inet</ipprotocol>
\t\t\t<defaultgw>on</defaultgw>
\t\t</gateway_item>
\t</gateways>""")

    wan_rule = """<rule>
\t\t\t<type>pass</type>
\t\t\t<ipprotocol>inet</ipprotocol>
\t\t\t<descr><![CDATA[Allow engine scoring + routed traffic to LAN]]></descr>
\t\t\t<interface>wan</interface>
\t\t\t<tracker>0100000201</tracker>
\t\t\t<source>
\t\t\t\t<any></any>
\t\t\t</source>
\t\t\t<destination>
\t\t\t\t<network>lan</network>
\t\t\t</destination>
\t\t</rule>
\t\t"""
    # The success probe is SSH engine->firewall WAN; pfSense blocks WAN-inbound by default, so without this it never answers.
    ssh_rule = f"""<rule>
\t\t\t<type>pass</type>
\t\t\t<ipprotocol>inet</ipprotocol>
\t\t\t<descr><![CDATA[Allow engine SSH to the firewall]]></descr>
\t\t\t<interface>wan</interface>
\t\t\t<tracker>0100000202</tracker>
\t\t\t<protocol>tcp</protocol>
\t\t\t<source>
\t\t\t\t<address>172.31.{tid}.1</address>
\t\t\t</source>
\t\t\t<destination>
\t\t\t\t<network>(self)</network>
\t\t\t\t<port>22</port>
\t\t\t</destination>
\t\t</rule>
\t\t"""
    x = x.replace("\t\t<rule>", "\t\t" + wan_rule + ssh_rule + "<rule>", 1)

    nat_rows = "\t\t\t<outbound>\n\t\t\t\t<mode>disabled</mode>\n\t\t\t</outbound>\n"
    # Services boxes reach at the gateway address live on the engine; once the firewall owns that address they must be redirected to the engine's transit address.
    for port, desc in ENGINE_GATEWAY_SERVICES:
        nat_rows += f"""\t\t\t<rule>
\t\t\t\t<interface>lan</interface>
\t\t\t\t<ipprotocol>inet</ipprotocol>
\t\t\t\t<protocol>tcp</protocol>
\t\t\t\t<source>
\t\t\t\t\t<any></any>
\t\t\t\t</source>
\t\t\t\t<destination>
\t\t\t\t\t<address>192.168.{tid}.1</address>
\t\t\t\t\t<port>{port}</port>
\t\t\t\t</destination>
\t\t\t\t<target>172.31.{tid}.1</target>
\t\t\t\t<local-port>{port}</local-port>
\t\t\t\t<descr><![CDATA[{desc} (engine service at the old gateway address)]]></descr>
\t\t\t\t<associated-rule-id></associated-rule-id>
\t\t\t</rule>\n"""
    for spec in red_dnat_spec or []:
        port, target = spec.split("->", 1)
        target = target.replace("{tid}", tid)
        nat_rows += f"""\t\t\t<rule>
\t\t\t\t<interface>lan</interface>
\t\t\t\t<ipprotocol>inet</ipprotocol>
\t\t\t\t<protocol>tcp/udp</protocol>
\t\t\t\t<source>
\t\t\t\t\t<any></any>
\t\t\t\t</source>
\t\t\t\t<destination>
\t\t\t\t\t<address>192.168.{tid}.1</address>
\t\t\t\t\t<port>{port.strip()}</port>
\t\t\t\t</destination>
\t\t\t\t<target>{target.strip()}</target>
\t\t\t\t<local-port>{port.strip()}</local-port>
\t\t\t\t<descr><![CDATA[beacon C2 DNAT (firewall owns the gateway address)]]></descr>
\t\t\t\t<associated-rule-id></associated-rule-id>
\t\t\t</rule>\n"""
    nat_block = f"<nat>\n{nat_rows}\t\t</nat>\n\t"
    x = x.replace("\t<filter>", "\t" + nat_block + "<filter>", 1)
    return x


def firewall_kind(template_name):
    return "vyos" if "vyos" in (template_name or "").lower() else "pfsense"


# VyOS pass A: everything on eth0, so the commit cannot cut the pushing session. Deliberately NO
# filter rules (live-validated on the 2026.03 image): the default-accept chains give pfSense's effect.
# Ends on a grep canary at the op-mode prompt then `exit` — ending at `exit` wedges the PTY.
def generate_vyos_commands(team_id, red_dnat_spec=None):
    tid = str(team_id)
    wan, gw, lan_gw = f"172.31.{tid}.2", f"172.31.{tid}.1", f"192.168.{tid}.1"
    lines = [
        "configure",
        f"set system host-name fw-team{tid}",
        "set system console device ttyS0 speed 115200",
        f"set interfaces ethernet eth0 address {wan}/30",
        f"set protocols static route 0.0.0.0/0 next-hop {gw}",
        "set service ssh port 22",
    ]
    dnat_rule = 10
    for port, desc in ENGINE_GATEWAY_SERVICES:
        lines += [
            f"set nat destination rule {dnat_rule} description '{desc} (engine service "
            f"at the old gateway address)'",
            f"set nat destination rule {dnat_rule} destination address {lan_gw}",
            f"set nat destination rule {dnat_rule} destination port {port}",
            f"set nat destination rule {dnat_rule} protocol tcp",
            f"set nat destination rule {dnat_rule} translation address {gw}",
            f"set nat destination rule {dnat_rule} translation port {port}",
        ]
        dnat_rule += 10
    for spec in red_dnat_spec or []:
        port, target = spec.split("->", 1)
        target = target.replace("{tid}", tid)
        lines += [
            "set nat destination rule %d description 'beacon C2 DNAT (firewall owns the "
            "gateway address)'" % dnat_rule,
            f"set nat destination rule {dnat_rule} destination address {lan_gw}",
            f"set nat destination rule {dnat_rule} destination port {port.strip()}",
            f"set nat destination rule {dnat_rule} protocol tcp_udp",
            f"set nat destination rule {dnat_rule} translation address {target.strip()}",
            f"set nat destination rule {dnat_rule} translation port {port.strip()}",
        ]
        dnat_rule += 10
    lines += [
        "commit",
        "save",
        "exit",
        "show configuration commands | grep host-name",
        "exit",
    ]
    return "\n".join(lines) + "\n"


# Pass B: run over the WAN address AFTER pass A — the session rides eth0 and survives the eth1 flip.
def generate_vyos_lan_commands(team_id):
    tid = str(team_id)
    return "\n".join([
        "configure",
        f"delete interfaces ethernet eth1 address {BOOTSTRAP_FW_IP}/24",
        f"set interfaces ethernet eth1 address 192.168.{tid}.1/24",
        "commit",
        "save",
        "exit",
        f"show interfaces ethernet eth1 | grep 192.168.{tid}.1",
        "exit",
    ]) + "\n"


def vyos_lan_config_path(pass_a_path):
    return pass_a_path.with_name(pass_a_path.stem + "-lan.cmds")


# Configs are kept as deploy artifacts — debugging a firewall means diffing what it was given.
def write_team_configs(comp_dir, teams, red_dnat_spec=None, kind="pfsense"):
    if kind == "vyos":
        out_dir = comp_dir / "vyos"
        out_dir.mkdir(exist_ok=True)
        out = {}
        for team_key, team in teams.items():
            tid = team["identifier"]
            path = out_dir / f"config-team{tid}.cmds"
            path.write_text(generate_vyos_commands(tid, red_dnat_spec))
            vyos_lan_config_path(path).write_text(generate_vyos_lan_commands(tid))
            out[team_key] = path
        return out
    seed_path = comp_dir / "pfsense" / "pfsense-config-orig.xml"
    if not seed_path.exists():
        raise SystemExit(
            f"  ERROR: {seed_path} is missing — the in-path firewall bootstrap needs the "
            "factory pfSense config.xml as its seed. Copy one from "
            "competitions/pfsense-ad/pfsense/pfsense-config-orig.xml (or export from any "
            "pfSense 2.7.x: Diagnostics > Backup & Restore) into competitions/"
            f"{comp_dir.name}/pfsense/.")
    seed = seed_path.read_text()
    key = config.active().ssh.public_key.strip()
    out_dir = comp_dir / "pfsense"
    out = {}
    for team_key, team in teams.items():
        path = out_dir / f"config-team{team['identifier']}.xml"
        path.write_text(generate_team_config(seed, team["identifier"], red_dnat_spec, key))
        out[team_key] = path
    return out


def tcp_probe_cmd(ip, port, timeout=4):
    return f"timeout {timeout} bash -c 'cat < /dev/null > /dev/tcp/{ip}/{port}' && echo UP"


def firewall_wan_ip(tid):
    return f"172.31.{tid}.2"


def team_gateway_ip(tid):
    return f"192.168.{tid}.1"


def _probe_tcp(ssh_ctx, ip, port, timeout=4):
    r = ssh_on_gateway(ssh_ctx, tcp_probe_cmd(ip, port, timeout), timeout=timeout + 10)
    return "UP" in (r.stdout or "")


def _wait_tcp(ssh_ctx, ip, port, budget_s=300, interval_s=10):
    deadline = time.time() + budget_s
    while True:
        if _probe_tcp(ssh_ctx, ip, port, timeout=8):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(interval_s)


def _push_command(config_xml):
    b64 = base64.b64encode(config_xml.encode()).decode()
    return (f"echo {b64} | openssl base64 -d -A > /tmp/tez-new.xml && [ -s /tmp/tez-new.xml ] && "
            "if cmp -s /tmp/tez-new.xml /cf/conf/config.xml; then echo UNCHANGED; "
            "else cp /tmp/tez-new.xml /cf/conf/config.xml && rm -f /tmp/config.cache && "
            "(nohup sh -c 'sleep 2; /sbin/reboot' >/dev/null 2>&1 &) && echo APPLIED; fi")


# An appliance still booting listens on sshd before a login can complete; the push is idempotent, so a retry after a half-run is safe.
def _push_config(ssh_ctx, ip, config_xml, who, budget_s=FW_BOOT_BUDGET_S):
    deadline = time.time() + budget_s
    last = "no attempt"
    while True:
        try:
            r = ssh_via_gateway(ssh_ctx, ip, _push_command(config_xml),
                                timeout=FW_PUSH_ATTEMPT_S, user=FW_USER)
            out = (r.stdout or "").strip().splitlines()[-1:]
            if r.returncode == 0 and out in (["APPLIED"], ["UNCHANGED"]):
                return out[0]
            last = f"rc={r.returncode} {(r.stderr or r.stdout or '').strip()[:200]}"
        except subprocess.TimeoutExpired:
            last = f"no answer within {FW_PUSH_ATTEMPT_S}s"
        if time.time() >= deadline:
            raise RuntimeError(f"{who}: config push to {ip} failed ({last})")
        time.sleep(10)


def _borrow_bootstrap_ip(ssh_ctx, nic, add):
    verb = "add" if add else "del"
    ssh_on_gateway(ssh_ctx, f"sudo ip addr {verb} {BOOTSTRAP_ENGINE_IP}/24 dev {nic} || true",
                   timeout=30)


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")


def _strip_ansi(text):
    return ANSI_RE.sub("", text).replace("\r", "")


# Feeding is expect-style — the next line goes in only after a prompt appears — because blind pacing
# loses twice on this image: bulk input wedges the CLI in `save`, and `save` flushes the tty input queue.
def _pty_session(args, script, timeout_s=45):
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        p = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave)
    finally:
        os.close(slave)
    buf = bytearray()
    deadline = time.time() + timeout_s
    lines = script.splitlines(keepends=True) or ["\n"]
    fed, marked, prompt_seen, last_output = 0, 0, True, time.time()
    while True:
        r, _, _ = select.select([master], [], [], 0.2)
        got = False
        if r:
            try:
                chunk = os.read(master, 8192)
            except OSError:              # EIO: session closed
                break
            if not chunk:
                break
            buf += chunk
            got = True
            last_output = time.time()
        clean = _strip_ansi(buf.decode(errors="replace"))
        prompt_seen = bool(re.search(r"[#$] $", clean[marked:]))
        if prompt_seen and fed < len(lines):
            os.write(master, lines[fed].encode())
            fed += 1
            marked = len(clean)
        if p.poll() is not None and fed >= len(lines):
            while select.select([master], [], [], 0.2)[0]:
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
            break
        # no output for 12s mid-script (wedged save), or all fed and quiet: treat as done
        if (not got and time.time() - last_output > 12 and fed >= len(lines)):
            break
        if not got and time.time() - last_output > 12 and time.time() > deadline:
            break
        if time.time() > deadline + 30:
            break
    if p.poll() is None:
        p.kill()
        p.wait()
    os.close(master)
    return p.returncode, buf.decode(errors="replace")


# No remote command — the login shell IS the CLI. A circinus commit prints NOTHING on success, so
# success = clean ssh exit AND the script's closing grep canary. Retried while the appliance boots.
def _push_vyos(ssh_ctx, ip, script, canary, who, budget_s=FW_BOOT_BUDGET_S):
    args = ["ssh", "-tt", "-i", ssh_ctx["ssh_key_path"],
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-o", "SetEnv=TERM=xterm",
            "-o", f"ProxyCommand={gateway_proxy(ssh_ctx)}",
            f"{FW_USER_VYOS}@{ip}"]
    deadline = time.time() + budget_s
    last = "no attempt"
    while True:
        rc, out = _pty_session(args, script, timeout_s=45)
        if rc == 0 and canary in out:
            return out
        last = f"rc={rc} {out.strip()[-200:]}"
        if time.time() >= deadline:
            raise RuntimeError(f"{who}: config push to {ip} failed ({last})")
        time.sleep(10)


def _bootstrap_vyos(ssh_ctx, target, pass_a_path, nic, log=print):
    tid, key, vmid = target["identifier"], target["team_key"], target["vmid"]
    wan = f"172.31.{tid}.2"
    pass_a = pass_a_path.read_text()
    if _probe_tcp(ssh_ctx, wan, 22):
        log(f"  {key}: firewall already on {wan} — pushing both passes there")
        _push_vyos(ssh_ctx, wan, pass_a, f"fw-team{tid}", key)
        _push_vyos(ssh_ctx, wan, vyos_lan_config_path(pass_a_path).read_text(),
                   f"192.168.{tid}.1", key)
        return
    log(f"  {key}: waiting for the template's SSH on {BOOTSTRAP_FW_IP} "
        f"(engine {nic}, vmid {vmid})")
    _borrow_bootstrap_ip(ssh_ctx, nic, add=True)
    try:
        if not _wait_tcp(ssh_ctx, BOOTSTRAP_FW_IP, 22, budget_s=FW_BOOT_BUDGET_S):
            raise RuntimeError(
                f"{key}: firewall vmid {vmid} never answered SSH on "
                f"{BOOTSTRAP_FW_IP} through engine {nic} — is it a clone of the "
                f"`vyos-provision` template (tools/build-vyos-provision-template.py) "
                f"with both NICs attached?")
        _push_vyos(ssh_ctx, BOOTSTRAP_FW_IP, pass_a, f"fw-team{tid}", key)
    finally:
        _borrow_bootstrap_ip(ssh_ctx, nic, add=False)
    if not _wait_tcp(ssh_ctx, wan, 22, budget_s=FW_APPLY_BUDGET_S):
        raise RuntimeError(
            f"{key}: firewall did not come up on {wan}:22 within {FW_APPLY_BUDGET_S}s "
            f"after the pass-A commit (live apply — this should be seconds). Remedy: "
            f"inspect the console (qm terminal / VNC) for vmid {vmid}, then re-run "
            f"--from-phase 5.")
    log(f"    {key}: WAN up on {wan} — flipping the LAN address (pass B, live commit)")
    _push_vyos(ssh_ctx, wan, vyos_lan_config_path(pass_a_path).read_text(),
               f"192.168.{tid}.1", key)
    log(f"    {key}: config applied live (no reboot)")


# Silent REST /agent/ping wait: clones of the pre-retrofit template answer False (normal), so the loud wait_for_guest_agent is for managed boxes.
def _agent_answers(node, vmid, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/agent/ping", data={})
            return True
        except Exception:
            time.sleep(5)
    return False


# Best-effort readback for a failed firewall wait; '' when the agent doesn't answer — diagnostics must never become the failure.
def fw_agent_diagnostic(node, vmid):
    if not _agent_answers(node, vmid, timeout=10):
        return ""
    try:
        rc, out, err = guest_agent_exec_root(
            node, vmid,
            "ifconfig 2>/dev/null | grep 'inet ' ; ls -l /cf/conf/config.xml; "
            "grep -c afterbootupshellcmd /cf/conf/config.xml",
            timeout=20, shell="sh")
        body = (out or err or "").strip()
        if not body:
            return ""
        return "\n    agent diagnostics (vmid %s):\n      " % vmid \
            + "\n      ".join(body.splitlines())
    except Exception:
        return ""


# Teams run one at a time: every template boots as 192.168.1.1 on its own isolated bridge, and the engine holds only one borrowed address route.
def bootstrap_firewalls(teams, fw_targets, config_paths, ssh_ctx, node, log=print):
    keys = sorted(teams)
    for t in fw_targets:
        tid, key = t["identifier"], t["team_key"]
        wan = f"172.31.{tid}.2"
        if firewall_kind((t.get("box") or {}).get("template", "")) == "vyos":
            _bootstrap_vyos(ssh_ctx, t, config_paths[key],
                            f"ens{19 + keys.index(key)}", log=log)
            continue
        config_xml = config_paths[key].read_text()
        if _probe_tcp(ssh_ctx, wan, 22):
            log(f"  {key}: firewall already on {wan} — pushing config there")
            result = _push_config(ssh_ctx, wan, config_xml, key)
        else:
            nic = f"ens{19 + keys.index(key)}"
            log(f"  {key}: waiting for the template's SSH on {BOOTSTRAP_FW_IP} "
                f"(engine {nic}, vmid {t['vmid']})")
            _borrow_bootstrap_ip(ssh_ctx, nic, add=True)
            try:
                if not _wait_tcp(ssh_ctx, BOOTSTRAP_FW_IP, 22, budget_s=FW_BOOT_BUDGET_S):
                    raise RuntimeError(
                        f"{key}: firewall vmid {t['vmid']} never answered SSH on "
                        f"{BOOTSTRAP_FW_IP} through engine {nic} — is it a clone of the "
                        f"`pfsense-provision` template (tools/build-pfsense-provision-"
                        f"template.py) with both NICs attached?"
                        + fw_agent_diagnostic(node, t["vmid"]))
                result = _push_config(ssh_ctx, BOOTSTRAP_FW_IP, config_xml, key)
            finally:
                _borrow_bootstrap_ip(ssh_ctx, nic, add=False)
        if result == "UNCHANGED":
            log(f"    {key}: config already current — no reboot")
            continue
        log(f"    {key}: config installed, firewall rebooting")
        if not _wait_tcp(ssh_ctx, wan, 22, budget_s=FW_APPLY_BUDGET_S):
            raise RuntimeError(
                f"{key}: firewall did not come back on {wan}:22 within {FW_APPLY_BUDGET_S}s "
                f"after the config push. Remedy: inspect the console (qm terminal / VNC) "
                f"for vmid {t['vmid']}, then re-run --from-phase 5."
                + fw_agent_diagnostic(node, t["vmid"]))
        log(f"    {key}: firewall up on {wan} (config applied)")


# NIC naming must match the engine's netplan: ens19+ are team NICs in sorted team-key order, then the transit NICs continue that sequence.
def cutover_netplan_yaml(teams, red_segment=None):
    keys = sorted(teams)
    n = len(keys)
    lines = ["network:", "  version: 2", "  ethernets:"]
    for idx, k in enumerate(keys):
        lines.append(f"    ens{18 + idx + 1}:")
        lines.append("      dhcp4: false")
        lines.append("      optional: true")
    for idx, k in enumerate(keys):
        tid = teams[k]["identifier"]
        lines.append(f"    ens{18 + n + idx + 1}:")
        lines.append(f"      addresses: [\"172.31.{tid}.1/30\"]")
        lines.append("      optional: true")
        lines.append("      routes:")
        lines.append(f"        - to: \"192.168.{tid}.0/24\"")
        lines.append(f"          via: \"172.31.{tid}.2\"")
    if red_segment:
        # The red segment's gateway lives on the engine, and it is the ONLY address
        # red01 can route the team subnets through. Dropping it here leaves red01
        # with a gateway that does not exist: every SSH the day-0 seed tries times
        # out, the seed plants nothing, and the deploy dies "no planted box" while
        # the rest of the range is healthy.
        lines.append(f"    ens{18 + n + n + 1}:")
        lines.append(f"      addresses: [\"{red_segment['gw']}/24\"]")
        lines.append("      optional: true")
    return "\n".join(lines) + "\n"


# The cutover rewrites the engine's netplan in place; nothing re-applies the pre-cutover file, so the team gateway address stays on the firewalls.
def cut_over_engine(ssh_ctx, teams, red_segment=None, log=print):
    log("  Engine cutover: 192.168.<team>.1 moves to the firewalls; engine routes "
        "team subnets via the transit /30s")
    b64 = base64.b64encode(cutover_netplan_yaml(teams, red_segment).encode()).decode()
    r = ssh_on_gateway(
        ssh_ctx,
        f"echo {b64} | base64 -d | sudo tee /etc/netplan/60-team-ifaces.yaml "
        f"> /dev/null && sudo chmod 600 /etc/netplan/60-team-ifaces.yaml "
        f"&& sudo netplan apply",
        timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"engine cutover failed (rc={r.returncode}): "
                           f"{(r.stderr or r.stdout or '').strip()[:300]}")


FW_INSPECT_CMD = {"pfsense": "pfctl -sr", "vyos": "show firewall"}


def _route_probe(ssh_ctx, tid):
    """One engine route probe: (ok, what the engine said)."""
    r = ssh_on_gateway(ssh_ctx, f"ip route get 192.168.{tid}.1", timeout=15)
    out = (r.stdout or "").strip()
    if f"via 172.31.{tid}.2" in out:
        return True, out
    # An empty answer is usually ssh landing while the cutover's `netplan apply` bounces the
    # engine's networking; keep rc/stderr so a post-mortem can tell that from a real route gap.
    detail = out[:80] or f"no output (ssh rc={r.returncode} {(r.stderr or '').strip()[:60]})"
    return False, detail


def verify_in_path(ssh_ctx, teams, managed_first_ip, log=print, kind="pfsense",
                   route_wait_s=120, poll_s=6):
    """Prove the range now scores through each team's firewall. The route check retries over a
    bounded window because the cutover ends with `netplan apply`, which restarts the engine's
    networking — a single immediate probe can read empty although the routes land moments later."""
    inspect = FW_INSPECT_CMD.get(kind, FW_INSPECT_CMD["pfsense"])
    deadline = time.time() + route_wait_s
    while True:
        failures = []
        for k in sorted(teams):
            tid = teams[k]["identifier"]
            ok, detail = _route_probe(ssh_ctx, tid)
            if not ok:
                failures.append(f"{k}: engine does not route 192.168.{tid}.0/24 via "
                                f"172.31.{tid}.2 (got: {detail})")
        if not failures or time.time() >= deadline:
            break
        left = int(deadline - time.time())
        log(f"  In-path verification: engine routes not converged yet ({left}s left) — retrying")
        time.sleep(poll_s)
    if failures:
        raise SystemExit(
            "  ERROR: in-path firewall verification failed:\n    - " + "\n    - ".join(failures)
            + f"\n  The range's scoring path runs through each team's firewall, so nothing "
              f"downstream can plant or score until this converges. Inspect with "
              f"`{inspect}` on the firewall (SSH, config enables it) and re-run "
              f"--from-phase 5.")
    failures = []
    for k in sorted(teams):
        tid = teams[k]["identifier"]
        box_ip = managed_first_ip.get(k)
        # The cutover just moved the gateway MAC: give ARP/routing a bounded window to converge.
        if box_ip and not _wait_tcp(ssh_ctx, box_ip, 22):
            failures.append(f"{k}: first managed box {box_ip}:22 not reachable through "
                            f"the firewall — check the WAN filter loaded ({inspect})")
    if failures:
        raise SystemExit(
            "  ERROR: in-path firewall verification failed:\n    - " + "\n    - ".join(failures)
            + f"\n  The range's scoring path runs through each team's firewall, so nothing "
              f"downstream can plant or score until this converges. Inspect with "
              f"`{inspect}` on the firewall (SSH, config enables it) and re-run "
              f"--from-phase 5.")
    log("  In-path verified: every team subnet routes via its firewall; boxes reachable")
