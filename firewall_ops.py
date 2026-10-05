"""In-path firewall plumbing: per-team pfSense config generation, SSH bootstrap, and the
engine cutover that moves the team gateway onto the firewall.

Per team <id> (the topology docs/pfsense-inpath-2026-09-28.md proved by hand and
deploy phase 5 now automates):

    engine ─transit 172.31.<id>.0/30 (vmbrW<id>)─ pfSense WAN 172.31.<id>.2/30
             pfSense LAN 192.168.<id>.1/24 (vmbr<id>) ─ boxes

The firewall boxes clone the `pfsense-provision` template (built once per node by
tools/build-pfsense-provision-template.py): SSH on, the deploy key in admin's
authorizedkeys, LAN vtnet1 = 192.168.1.1/24. Phase 5 therefore needs no console: the
engine borrows 192.168.1.2/24 on one team NIC at a time, SSH goes through the engine to
192.168.1.1, the per-team config.xml replaces /cf/conf/config.xml, and the firewall reboots
into its WAN/LAN addressing. The generated config enables SSH and carries the same key, so a
re-run reaches the firewall on its transit address instead — and skips the reboot when the
config on the box already matches.

The seed config is the comp's `pfsense/pfsense-config-orig.xml` (every firewall comp
carries one — copy from competitions/pfsense-ad/pfsense/ to start a new one). There is
no pipeline-shipped default on purpose: the generator's string surgery anchors on the
factory config's exact stanzas, and a hand-rolled seed would fail silently mid-deploy.
"""

import base64
import os
import time

from ssh_ops import ssh_on_gateway, ssh_via_gateway

# (port, label) of engine services boxes reach at the team gateway address. apt-cacher-ng
# is the one every box needs (prep_apt's apt proxy points at 192.168.<id>.1:3142).
ENGINE_GATEWAY_SERVICES = ((3142, "apt-cacher"),)
# The provisioning template's LAN address, and the one the engine borrows beside it.
BOOTSTRAP_FW_IP = "192.168.1.1"
BOOTSTRAP_ENGINE_IP = "192.168.1.2"
FW_USER = "admin"            # uid 0 on pfSense; the deploy key is in its authorizedkeys
FW_BOOT_BUDGET_S = 420       # a cloned appliance's first boot to SSH
FW_APPLY_BUDGET_S = 300      # config push → reboot → SSH on the WAN address


def generate_team_config(seed_xml, team_id, red_dnat_spec=None, authorized_key=None):
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
    replaced with the team identifier for per-team routed-segment targets.

    authorized_key (an OpenSSH public key line) lands in admin's authorizedkeys so the
    deploy can SSH the firewall after the config replaces the template's."""
    tid = str(team_id)
    x = seed_xml

    x = x.replace("<ssh></ssh>", "<ssh><enable>enabled</enable></ssh>")
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
    # The deploy's success probe (and later admin access) is SSH from the engine to the
    # firewall's own WAN address. pfSense blocks WAN-inbound to itself by default and the
    # rule above only matches traffic destined to the LAN, so without this the probe can
    # never answer even though the config applied (live-found 2026-10-05, pfsense-ad).
    # Source is pinned to the engine's transit address: the transit /30 has no other host.
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
    # Services the boxes reach at the gateway address (apt-cacher via the 95proxy
    # apt.conf) live on the engine; once the firewall owns that address they must be
    # redirected to the engine's transit address (live-found 2026-10-05: every Linux
    # domain-join failed on `Unable to connect to 192.168.<id>.1:3142`).
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


def write_team_configs(comp_dir, teams, red_dnat_spec=None):
    """Write config-team<id>.xml for every team into the comp's pfsense/ dir (kept as
    deploy artifacts — debugging a firewall means diffing what it was given).
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
    key = os.environ.get("TF_VAR_ssh_public_key", "").strip()
    out_dir = comp_dir / "pfsense"
    out = {}
    for team_key, team in teams.items():
        path = out_dir / f"config-team{team['identifier']}.xml"
        path.write_text(generate_team_config(seed, team["identifier"], red_dnat_spec, key))
        out[team_key] = path
    return out


def tcp_probe_cmd(ip, port, timeout=4):
    """Remote shell probe (run on the engine): prints UP when ip:port accepts a connection."""
    return f"timeout {timeout} bash -c 'cat < /dev/null > /dev/tcp/{ip}/{port}' && echo UP"


def firewall_wan_ip(tid):
    """The firewall's transit-side (WAN) address for team identifier `tid`."""
    return f"172.31.{tid}.2"


def team_gateway_ip(tid):
    """The team gateway address: held by the engine pre-cutover, by the firewall after."""
    return f"192.168.{tid}.1"


def _probe_tcp(ssh_ctx, ip, port, timeout=4):
    r = ssh_on_gateway(ssh_ctx, tcp_probe_cmd(ip, port, timeout), timeout=timeout + 10)
    return "UP" in (r.stdout or "")


def _wait_tcp(ssh_ctx, ip, port, budget_s=300, interval_s=10):
    """_probe_tcp, retried until `budget_s` elapses (convergence after a network change)."""
    deadline = time.time() + budget_s
    while True:
        if _probe_tcp(ssh_ctx, ip, port, timeout=8):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(interval_s)


def _push_command(config_xml):
    """Shell line (pfSense admin shell) that installs config_xml and reboots — or reports
    UNCHANGED and does nothing when the box already runs exactly this config."""
    b64 = base64.b64encode(config_xml.encode()).decode()
    return (f"echo {b64} | openssl base64 -d -A > /tmp/tez-new.xml && [ -s /tmp/tez-new.xml ] && "
            "if cmp -s /tmp/tez-new.xml /cf/conf/config.xml; then echo UNCHANGED; "
            "else cp /tmp/tez-new.xml /cf/conf/config.xml && rm -f /tmp/config.cache && "
            "(nohup sh -c 'sleep 2; /sbin/reboot' >/dev/null 2>&1 &) && echo APPLIED; fi")


def _push_config(ssh_ctx, ip, config_xml, who):
    r = ssh_via_gateway(ssh_ctx, ip, _push_command(config_xml), timeout=90, user=FW_USER)
    out = (r.stdout or "").strip()
    if r.returncode != 0 or out.splitlines()[-1:] not in (["APPLIED"], ["UNCHANGED"]):
        raise RuntimeError(f"{who}: config push to {ip} failed (rc={r.returncode}): "
                           f"{(r.stderr or out)[:300]}")
    return out.splitlines()[-1]


def _borrow_bootstrap_ip(ssh_ctx, nic, add):
    verb = "add" if add else "del"
    ssh_on_gateway(ssh_ctx, f"sudo ip addr {verb} {BOOTSTRAP_ENGINE_IP}/24 dev {nic} || true",
                   timeout=30)


def bootstrap_firewalls(teams, fw_targets, config_paths, ssh_ctx, log=print):
    """Push every team's generated config to its firewall over SSH, then wait for the
    appliance to answer on its WAN address.

    fw_targets: one target dict per team's in_path firewall (slot-0 only — the engine
    node), each carrying team_key/identifier. config_paths: team_key → the generated
    config-team<id>.xml. Teams run one at a time: every template boots as 192.168.1.1 on
    its own isolated bridge, and the engine can hold only one borrowed address route at
    once. Idempotent — a firewall already answering on its transit address is reached
    there and left alone when its config is unchanged."""
    keys = sorted(teams)
    for t in fw_targets:
        tid, key = t["identifier"], t["team_key"]
        wan = f"172.31.{tid}.2"
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
                        f"template.py) with both NICs attached?")
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
                f"for vmid {t['vmid']}, then re-run --from-phase 5.")
        log(f"    {key}: firewall up on {wan} (config applied)")


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
        # The cutover just moved the gateway MAC: give ARP/routing a bounded window to
        # converge instead of failing on the first probe (live-found 2026-10-05).
        if box_ip and not _wait_tcp(ssh_ctx, box_ip, 22):
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
