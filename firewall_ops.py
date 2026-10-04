"""In-path firewall plumbing: per-team pfSense config generation, console bootstrap,
and the engine cutover that moves the team gateway onto the firewall.

Per team <id> (the topology docs/pfsense-inpath-2026-09-28.md proved by hand and
deploy phase 5 now automates):

    engine ─transit 172.31.<id>.0/30 (vmbrW<id>)─ pfSense WAN 172.31.<id>.2/30
             pfSense LAN 192.168.<id>.1/24 (vmbr<id>) ─ boxes

The pfSense template predates any provisionable first boot (ZFS pool the host must not
import — see the runbook's trap list), so configuration rides the CONSOLE: option 8
shell, a temp address on the LAN NIC, fetch of the per-team config.xml from the engine
(which still owns the team gateway address at that point), reboot. Keystrokes go through
the QEMU monitor API (`sendkey`); success is judged functionally — the fetched config
enables SSH, so each firewall is probed on its WAN address afterward. A failed team is
re-driven from the menu; the sequence is idempotent.

The seed config is the comp's `pfsense/pfsense-config-orig.xml` (every firewall comp
carries one — copy from competitions/pfsense-ad/pfsense/ to start a new one). There is
no pipeline-shipped default on purpose: the generator's string surgery anchors on the
factory config's exact stanzas, and a hand-rolled seed would fail silently mid-deploy.
"""

import base64
import time

from range_ops import proxmox_api
from ssh_ops import ssh_on_gateway

# The engine-side HTTP server that serves config-team<id>.xml to the consoles. Uncommon
# port: the engine already runs the quotient stack (80/443), apt-cacher (3142), postgres
# and redis, and the port is typed character-by-character into a console.
FW_CONFIG_PORT = 8611
# pfSense 2.7.2 boots to its console menu in well under a minute; this is the settle
# wait before the first (and every re-) drive of the menu, so keystrokes never land in
# the FreeBSD loader where they would abort autoboot.
CONSOLE_SETTLE_S = 90
SSH_PROBE_BUDGET_S = 600

# QEMU sendkey names for the characters the console commands need. Everything else
# printable raises — the fetch URL is machine-built, so there is nothing legitimate
# outside this set, and a silent wrong key would strand a firewall mid-bootstrap.
_KEY_NAMES = {".": "dot", "/": "slash", "-": "minus", "_": "shift-minus",
              ",": "comma", "=": "equal", " ": "spc", "\n": "ret",
              ":": "shift-semicolon", "@": "shift-2"}


def key_sequence(text):
    """Text → QEMU sendkey tokens, one per keystroke (shift-combos for the shifted few)."""
    out = []
    for ch in text:
        if ch.isdigit() or "a" <= ch <= "z":
            out.append(ch)
        elif ch in _KEY_NAMES:
            out.append(_KEY_NAMES[ch])
        elif "A" <= ch <= "Z":
            out.append(f"shift-{ch.lower()}")
        else:
            raise ValueError(f"no sendkey mapping for {ch!r} in {text!r} — console "
                             "commands stay within [a-z0-9 .:/@_-] (firewall_ops._KEY_NAMES)")
    return out


def generate_team_config(seed_xml, team_id, red_dnat_spec=None):
    """Render one team's pfSense config.xml from the factory seed (pure string surgery,
    promoted from the per-comp gen_pfsense_config.py copies).

    WAN vtnet0 = 172.31.<team_id>.2/30 (gw = engine 172.31.<team_id>.1); LAN vtnet1 =
    192.168.<team_id>.1/24; SSH on; outbound NAT disabled (the engine masquerades to
    the internet); WAN pass rule any→lan so routed engine scoring gets in. The WAN rule
    must be the FIRST <rule> in the filter block and its destination must be the
    `lan` KEYWORD — pfSense's <network> field takes a keyword/alias, not a CIDR, and a
    raw CIDR there silently drops the rule (pfsense-ad 2026-09-28).

    red_dnat_spec (optional, Compfile `firewall_dnat` "PORT->TARGET[,...]") adds port
    forwards on the LAN address — the beacon-C2 path when the firewall, not the engine,
    owns the gateway address and the beacon lands on .1. `{tid}` in the target is
    replaced with the team identifier for per-team routed-segment targets."""
    tid = str(team_id)
    x = seed_xml

    x = x.replace("<ssh></ssh>", "<ssh><enable>enabled</enable></ssh>")
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
    x = x.replace("\t\t<rule>", "\t\t" + wan_rule + "<rule>", 1)

    nat_rows = "\t\t\t<outbound>\n\t\t\t\t<mode>disabled</mode>\n\t\t\t</outbound>\n"
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


def write_team_configs(comp_dir, teams, red_dnat_spec=None):
    """Write config-team<id>.xml for every team into the comp's pfsense/ dir (kept as
    deploy artifacts — debugging a stuck console means diffing what the firewall got).
    Returns the seed-missing error when there is no seed to work from."""
    seed_path = comp_dir / "pfsense" / "pfsense-config-orig.xml"
    if not seed_path.exists():
        raise SystemExit(
            f"  ERROR: {seed_path} is missing — the in-path firewall bootstrap needs the "
            "factory pfSense config.xml as its seed. Copy one from "
            "competitions/pfsense-ad/pfsense/pfsense-config-orig.xml (or export from any "
            "pfSense 2.7.x: Diagnostics > Backup & Restore) into competitions/"
            f"{comp_dir.name}/pfsense/.")
    seed = seed_path.read_text()
    out_dir = comp_dir / "pfsense"
    out = {}
    for team_key, team in teams.items():
        path = out_dir / f"config-team{team['identifier']}.xml"
        path.write_text(generate_team_config(seed, team["identifier"], red_dnat_spec))
        out[team_key] = path
    return out


# ── console driving ──────────────────────────────────────────────────────────────────────

def _monitor(node, vmid, command):
    """One raw QEMU monitor command via the API (sendkey needs no VNC channel)."""
    return proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/monitor",
                       data={"command": command})


def type_into_console(node, vmid, text, inter_key_s=0.05):
    """Type one line into the VM's console and press Enter. Blind by design: the
    verification is functional (the fetched config enables SSH), not screen-reading."""
    for key in key_sequence(text):
        _monitor(node, vmid, f"sendkey {key}")
        time.sleep(inter_key_s)
    _monitor(node, vmid, "sendkey ret")


def console_screenshot(node, vmid, out_path):
    """Best-effort PNG of the console (the runbook's screendump, operator-facing).

    Used when a firewall bootstrap gives up, so the human can see what the driver
    could not: a loader prompt, a menu in the wrong state, a fetch error. Failures
    here are swallowed — the bootstrap error itself is what must propagate."""
    try:
        import ssl
        import struct
        from urllib.parse import quote

        import requests
        import urllib3
        import websocket
        from PIL import Image
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        urllib3.disable_warnings()
        import os
        endpoint = os.environ["TF_VAR_proxmox_endpoint"].rstrip("/")
        host = endpoint.split("//")[1].split(":")[0]
        auth = {"Authorization": f"PVEAPIToken={os.environ['TF_VAR_proxmox_api_token']}"}
        s = requests.Session()
        s.headers.update(auth)
        s.verify = False
        d = s.post(f"{endpoint}/api2/json/nodes/{node}/qemu/{vmid}/vncproxy",
                   data={"websocket": 1}, timeout=30).json()["data"]
        ws = websocket.create_connection(
            f"wss://{host}:8006/api2/json/nodes/{node}/qemu/{vmid}/vncwebsocket"
            f"?port={d['port']}&vncticket={quote(d['ticket'])}",
            header={"Authorization": auth["Authorization"]},
            sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=60)

        def rx(n):
            buf = b""
            while len(buf) < n:
                chunk = ws.recv()
                if not chunk:
                    raise EOFError("websocket closed")
                buf += chunk
            return buf

        rx(12)
        ws.send_binary(b"RFB 003.003\n")
        sectype = struct.unpack(">I", rx(4))[0]
        if sectype == 2:
            challenge = rx(16)
            key = d["ticket"].encode()[:8].ljust(8, b"\x00")
            des_key = bytes(int(f"{b:08b}"[::-1], 2) for b in key)
            enc = Cipher(algorithms.TripleDES(des_key), modes.ECB()).encryptor()
            ws.send_binary(enc.update(challenge) + enc.finalize())
            if struct.unpack(">I", rx(4))[0] != 0:
                raise OSError("VNC auth failed")
        elif sectype != 1:
            raise OSError(f"unsupported security type {sectype}")
        ws.send_binary(b"\x01")
        init = rx(24)
        w, h = struct.unpack(">HH", init[:4])
        pf = bytes([0, 0, 0, 0, 32, 24, 0, 0, 0, 0, 0, 0,
                    255, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0])
        ws.send_binary(b"\x00" + pf)
        ws.send_binary(b"\x02" + b"\x00" + struct.pack(">H", 1) + struct.pack(">i", 0))
        ws.send_binary(b"\x03" + b"\x00" + struct.pack(">HHHH", 0, 0, w, h))
        hdr = rx(16)
        px = rx(struct.unpack(">I", hdr[12:16])[0])
        Image.frombytes("RGBX", (w, h), px).convert("RGB").save(out_path)
        ws.close()
        return str(out_path)
    except Exception as e:  # noqa: BLE001 — diagnostics must never mask the real failure
        return f"(screenshot failed: {e})"


def _probe_tcp(ssh_ctx, ip, port, timeout=4):
    r = ssh_on_gateway(
        ssh_ctx,
        f"timeout {timeout} bash -c 'cat < /dev/null > /dev/tcp/{ip}/{port}' && echo UP",
        timeout=timeout + 10)
    return "UP" in (r.stdout or "")


def _drive_console(node, vmid, identifier, config_url):
    """One full menu→shell→fetch→reboot drive. Runs only after CONSOLE_SETTLE_S, so a
    booting system is never interrupted mid-loader."""
    time.sleep(3)  # let any pending console output flush before we take the keyboard
    type_into_console(node, vmid, "")            # Enter — dismiss a stale screen safely
    time.sleep(1)
    type_into_console(node, vmid, "8")           # pfSense console menu → 8 = Shell
    time.sleep(3)
    type_into_console(node, vmid,
                      f"ifconfig vtnet1 inet 192.168.{identifier}.250/24 up")
    time.sleep(2)
    type_into_console(node, vmid, f"fetch -o /cf/conf/config.xml {config_url}")
    time.sleep(12)  # fetch + config apply on the appliance's own disk
    type_into_console(node, vmid, "reboot")


def bootstrap_firewalls(node, teams, fw_targets, config_paths, ssh_ctx,
                        red_dnat_spec=None, comp_name="", log=print):
    """Drive every team's firewall console to fetch its config, then wait for the
    appliance to come up on its WAN address.

    fw_targets: one target dict per team's in_path firewall (slot-0 only — the engine
    node), each carrying team_key/identifier/vmid/node. config_paths: team_key → the
    generated config-team<id>.xml (served from the engine over HTTP). Idempotent: every
    re-drive re-fetches the same config and reboots."""
    dnat_note = f" (+{len(red_dnat_spec or [])} DNAT)" if red_dnat_spec else ""
    engine_dir = f"/tmp/tez-fwcfg-{comp_name}"
    quoted = " ".join(f"{k}:{v.name}" for k, v in config_paths.items())
    log(f"  Serving {quoted} from the engine on :{FW_CONFIG_PORT}{dnat_note}")
    ssh_on_gateway(ssh_ctx, f"rm -rf {engine_dir} && mkdir -p {engine_dir}", timeout=30)
    for team_key, path in config_paths.items():
        b64 = base64.b64encode(path.read_bytes()).decode()
        ssh_on_gateway(ssh_ctx,
                       f"echo {b64} | base64 -d | sudo tee {engine_dir}/{path.name} "
                       f"> /dev/null && sudo chmod 644 {engine_dir}/{path.name}",
                       timeout=60)
    try:
        ssh_on_gateway(ssh_ctx,
                       f"nohup python3 -m http.server {FW_CONFIG_PORT} --directory "
                       f"{engine_dir} > {engine_dir}/http.log 2>&1 & echo started",
                       timeout=30)
        time.sleep(2)

        for t in fw_targets:
            tid = t["identifier"]
            url = f"http://192.168.{tid}.1:{FW_CONFIG_PORT}/config-team{tid}.xml"
            log(f"  Bootstrapping {t['team_key']}'s firewall (vmid {t['vmid']}, "
                f"WAN 172.31.{tid}.2)... console drive → fetch → reboot")
            deadline = time.time() + SSH_PROBE_BUDGET_S
            attempt = 0
            while True:
                attempt += 1
                log(f"    console drive #{attempt} (settle {CONSOLE_SETTLE_S}s first)")
                _drive_console(node, t["vmid"], tid, url)
                # One full boot cycle worth of probing per drive: the fetched config
                # enables SSH, so an answer on the WAN address IS the success signal.
                cycle_end = min(deadline, time.time() + CONSOLE_SETTLE_S + 120)
                while time.time() < cycle_end and \
                        not _probe_tcp(ssh_ctx, f"172.31.{tid}.2", 22):
                    time.sleep(20)
                if _probe_tcp(ssh_ctx, f"172.31.{tid}.2", 22):
                    log(f"    {t['team_key']}: firewall up — SSH answering on "
                        f"172.31.{tid}.2 (config applied)")
                    break
                if time.time() >= deadline:
                    shot = console_screenshot(node, t["vmid"],
                                              f"logs/fw-console-{comp_name}-{tid}.png")
                    raise RuntimeError(
                        f"firewall bootstrap for team {t['team_key']} (vmid {t['vmid']}) "
                        f"never came up on 172.31.{tid}.2:22 after {attempt} drive(s) — "
                        f"the console fetch/reboot did not take. Console screenshot: "
                        f"{shot}. Remedy: check the fetch URL was reachable (engine HTTP "
                        f"server on :{FW_CONFIG_PORT}), then re-run --from-phase 5.")
                log("    not up yet — re-driving the console (the drive is idempotent)")
    finally:
        ssh_on_gateway(ssh_ctx,
                       f"pkill -f 'http.server {FW_CONFIG_PORT}' ; rm -rf {engine_dir}",
                       timeout=30)


def cutover_netplan_yaml(teams):
    """The post-cutover /etc/netplan/60-team-ifaces.yaml body (pure, for tests).

    NIC naming must match terraform's team_nics exactly: ens19+ are the team NICs in
    sorted team key order, the transit NICs continue the same positional sequence (one
    per team, same order). Team NICs stay up but carry no address (the firewall owns
    the gateway now); each transit NIC holds 172.31.<id>.1/30 and the route to its
    team subnet via 172.31.<id>.2."""
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
    return "\n".join(lines) + "\n"


def cut_over_engine(ssh_ctx, teams, log=print):
    """Move every team's gateway address from the engine to its firewall.

    Rewrites /etc/netplan/60-team-ifaces.yaml (the file terraform's team_nics
    provisioner wrote — see cutover_netplan_yaml), then `netplan apply` — no reboot,
    the NICs already exist. Idempotent. CAVEAT (docs/internals.md): a future
    `terraform apply` re-writes the pre-cutover file."""
    log("  Engine cutover: 192.168.<team>.1 moves to the firewalls; engine routes "
        "team subnets via the transit /30s")
    b64 = base64.b64encode(cutover_netplan_yaml(teams).encode()).decode()
    r = ssh_on_gateway(
        ssh_ctx,
        f"echo {b64} | base64 -d | sudo tee /etc/netplan/60-team-ifaces.yaml "
        f"> /dev/null && sudo chmod 600 /etc/netplan/60-team-ifaces.yaml "
        f"&& sudo netplan apply",
        timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"engine cutover failed (rc={r.returncode}): "
                           f"{(r.stderr or r.stdout or '').strip()[:300]}")


def verify_in_path(ssh_ctx, teams, managed_first_ip, log=print):
    """Fail-loud convergence gate after the cutover (routing_ops style): the engine
    must route every team subnet via its firewall, each firewall must answer on its
    transit address, and the first managed box must be reachable THROUGH it (the path
    every later plant/scoring step takes)."""
    failures = []
    for k in sorted(teams):
        tid = teams[k]["identifier"]
        if not _probe_tcp(ssh_ctx, f"172.31.{tid}.2", 22):
            failures.append(f"{k}: firewall WAN 172.31.{tid}.2:22 unreachable from engine")
            continue
        r = ssh_on_gateway(ssh_ctx, f"ip route get 192.168.{tid}.1", timeout=15)
        if f"via 172.31.{tid}.2" not in (r.stdout or ""):
            failures.append(f"{k}: engine does not route 192.168.{tid}.0/24 via "
                            f"172.31.{tid}.2 (got: {(r.stdout or '').strip()[:80]})")
            continue
        box_ip = managed_first_ip.get(k)
        if box_ip and not _probe_tcp(ssh_ctx, box_ip, 22, timeout=8):
            failures.append(f"{k}: first managed box {box_ip}:22 not reachable through "
                            "the firewall — check the WAN pass rule loaded (pfctl -sr)")
    if failures:
        raise SystemExit(
            "  ERROR: in-path firewall verification failed:\n    - " + "\n    - ".join(failures)
            + "\n  The range's scoring path runs through each team's firewall, so nothing "
              "downstream can plant or score until this converges. Inspect with "
              "`pfctl -sr` on the firewall (SSH, config enables it) and re-run "
              "--from-phase 5.")
    log("  In-path verified: every team subnet routes via its firewall; boxes reachable")
