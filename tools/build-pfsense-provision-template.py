#!/usr/bin/env python3
"""Build the provisioning-friendly pfSense template (`pfsense-provision`) from the stock
`pfsense` template, ONCE per node.

The stock template (956) boots to a VGA menu with SSH off and no LAN address, which is why
phase 5 used to type a fetch command at the console. This builder types the equivalent
edits ONE time, so every later deploy is `wait for SSH, push config`:

  * SSH enabled, the deploy public key in admin's authorizedkeys (admin is uid 0)
  * LAN = vtnet1 192.168.1.1/24 (the bootstrap address; phase 5 replaces the whole config)
  * serial console enabled (`<enableserial/>`; the VM gets serial0 so `qm terminal` works)

Usage (from a worktree root with the TARGET NODE's env exported — the retrofit targets
.150, so `set -a; source .env.cyberrange-20260930; set +a` first; load_dotenv won't
override what's already set):
  tools/build-pfsense-provision-template.py clone        # full-clone 956 -> NEWID, start it
  tools/build-pfsense-provision-template.py edit         # type the edits at the console
  tools/build-pfsense-provision-template.py shot OUT     # console PNG (verify by eye)
  tools/build-pfsense-provision-template.py seal         # halt, serial0, convert to template

qemu-guest-agent retrofit (2026-10-08) — adds the FreeBSD qemu-guest-agent to the sealed
`pfsense-provision` template WITHOUT touching the console: it full-clones the template to
a scratch VM, reaches the guest's template SSH (192.168.1.1 on a node-side bridge alias,
the host-level equivalent of phase 5's engine borrow), stages the .pkg dependency closure
from pkg.freebsd.org offline-installation style (the range has no DNS/internet), installs,
and reseals under the canonical name (the old template is renamed to a -agentless-bak).
Terraform already sets `agent { enabled = true }` on firewall clones — this supplies the
guest half:

  tools/build-pfsense-provision-template.py agent-clone   # clone NEWID -> QGA_VMID, agent=1, start
  tools/build-pfsense-provision-template.py agent-install # download pkgs, push+install, verify ping
  tools/build-pfsense-provision-template.py agent-seal    # halt, rename-swap, convert to template
"""

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv(".env")

from pve_api import proxmox_api, wait_for_proxmox_task  # noqa: E402

SOURCE_VMID = 956
NEWID = int(os.environ.get("TEZ_PFSENSE_PROVISION_VMID", "957"))
NAME = "pfsense-provision"
BOOTSTRAP_LAN = "192.168.1.1"
PUBKEY_FILE = Path("proxmox.pub")

_KEYS = {".": "dot", "/": "slash", "-": "minus", "_": "shift-minus", ",": "comma",
         "=": "equal", " ": "spc", ":": "shift-semicolon", "@": "shift-2",
         "|": "shift-backslash", "<": "shift-comma", ">": "shift-dot", "'": "apostrophe",
         '"': "shift-apostrophe", "+": "shift-equal", ";": "semicolon", "&": "shift-7"}


def _monitor(vmid, command):
    return proxmox_api("POST", f"/nodes/{node()}/qemu/{vmid}/monitor", data={"command": command})


def console_screenshot(vmid, out_path):
    """PNG of the VM's VGA console via the PVE vncwebsocket (raw RFB, 3.3-style handshake:
    PVE rejects the 3.8 type-list path over vncwebsocket). PVE packs several RFB messages
    into one websocket frame, so reads are buffered, not per-frame."""
    import ssl
    import struct
    from urllib.parse import quote

    import requests
    import urllib3
    import websocket
    from PIL import Image
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    urllib3.disable_warnings()
    endpoint = os.environ["TF_VAR_proxmox_endpoint"].rstrip("/")
    host = endpoint.split("//")[1].split(":")[0]
    auth = {"Authorization": f"PVEAPIToken={os.environ['TF_VAR_proxmox_api_token']}"}
    sess = requests.Session()
    sess.headers.update(auth)
    sess.verify = False
    d = sess.post(f"{endpoint}/api2/json/nodes/{node()}/qemu/{vmid}/vncproxy",
                  data={"websocket": 1}, timeout=30).json()["data"]
    ws = websocket.create_connection(
        f"wss://{host}:8006/api2/json/nodes/{node()}/qemu/{vmid}/vncwebsocket"
        f"?port={d['port']}&vncticket={quote(d['ticket'])}",
        header=auth, sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=60)
    pending = bytearray()

    def rx(n):
        while len(pending) < n:
            pending.extend(ws.recv())
        out = bytes(pending[:n])
        del pending[:n]
        return out

    try:
        rx(12)
        ws.send_binary(b"RFB 003.003\n")
        if struct.unpack(">I", rx(4))[0] == 2:
            key = d["ticket"].encode()[:8].ljust(8, b"\x00")
            des = bytes(int(f"{b:08b}"[::-1], 2) for b in key)
            enc = Cipher(algorithms.TripleDES(des), modes.ECB()).encryptor()
            ws.send_binary(enc.update(rx(16)) + enc.finalize())
            if struct.unpack(">I", rx(4))[0] != 0:
                raise OSError("VNC auth failed")
        ws.send_binary(b"\x01")
        init = rx(24)
        w, h = struct.unpack(">HH", init[:4])
        rx(struct.unpack(">I", init[20:24])[0])
        pf = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        ws.send_binary(b"\x00\x00\x00\x00" + pf)
        ws.send_binary(b"\x02\x00" + struct.pack(">Hi", 1, 0))
        ws.send_binary(b"\x03\x00" + struct.pack(">HHHH", 0, 0, w, h))
        rx(16)
        Image.frombytes("RGBX", (w, h), rx(w * h * 4), "raw", "BGRX").convert("RGB").save(out_path)
        return out_path
    finally:
        ws.close()


def node():
    return os.environ["TF_VAR_proxmox_node"]


def type_line(text, inter_key_s=0.04):
    for ch in text:
        if ch.isdigit() or "a" <= ch <= "z":
            key = ch
        elif "A" <= ch <= "Z":
            key = f"shift-{ch.lower()}"
        elif ch in _KEYS:
            key = _KEYS[ch]
        else:
            raise ValueError(f"no sendkey mapping for {ch!r}")
        _monitor(NEWID, f"sendkey {key}")
        time.sleep(inter_key_s)
    _monitor(NEWID, "sendkey ret")
    time.sleep(0.5)


def clone():
    n = node()
    up = proxmox_api("POST", f"/nodes/{n}/qemu/{SOURCE_VMID}/clone", data={
        "newid": NEWID, "name": NAME, "full": 1,
        "description": "pfsense-provision: stock pfsense + SSH/LAN/serial (build-pfsense-provision-template.py)"})
    wait_for_proxmox_task(n, up["data"])
    proxmox_api("POST", f"/nodes/{n}/qemu/{NEWID}/status/start")
    print(f"cloned {SOURCE_VMID} -> {NEWID}, started; wait ~90s for the menu")


def edit():
    import base64
    key_b64 = base64.b64encode(PUBKEY_FILE.read_bytes().strip() + b"\n").decode()
    cfg = "/cf/conf/config.xml"
    lan = (f"<lan><enable></enable><if>vtnet1</if><ipaddr>{BOOTSTRAP_LAN}</ipaddr>"
           f"<subnet>24</subnet></lan>")
    type_line("")
    time.sleep(1)
    type_line("8")
    time.sleep(3)
    type_line(f"sed -i .bak -e 's,<ssh></ssh>,<ssh><enable>enabled</enable></ssh>,' {cfg}")
    type_line(f"sed -i '' -e 's,</wan>,</wan>{lan},' {cfg}")
    type_line(f"sed -i '' -e 's,<name>admin</name>,<name>admin</name>"
              f"<authorizedkeys>{key_b64}</authorizedkeys>,' {cfg}")
    type_line(f"sed -i '' -e 's,<system>,<system><enableserial></enableserial>,' {cfg}")
    type_line(f"rm -f /tmp/config.cache; grep -c -e enableserial -e authorizedkeys -e {BOOTSTRAP_LAN} {cfg}")
    print("typed; check with `shot`")


def seal():
    n = node()
    type_line("halt -p")
    for _ in range(40):
        time.sleep(5)
        if proxmox_api("GET", f"/nodes/{n}/qemu/{NEWID}/status/current")["data"]["status"] == "stopped":
            break
    else:
        raise SystemExit("VM did not power off")
    proxmox_api("PUT", f"/nodes/{n}/qemu/{NEWID}/config", data={"serial0": "socket", "tags": "template"})
    proxmox_api("POST", f"/nodes/{n}/qemu/{NEWID}/template")
    print(f"{NEWID} sealed as a template")


# ---------------------------------------------------------------------------
# qemu-guest-agent retrofit


FW_ADMIN = "admin"          # uid 0 on pfSense; the deploy key (proxmox.pub) is in its authorizedkeys
QGA_VMID = int(os.environ.get("TEZ_PFSENSE_QGA_VMID", "958"))
QGA_WORK_NAME = "pfsense-provision-qga"
QGA_BACKUP_NAME = "pfsense-provision-agentless-bak"
QGA_BRIDGE = os.environ.get("TEZ_QGA_BRIDGE", "vmbr0")
QGA_ALIAS_IP = "192.168.1.2"                    # aliased on QGA_BRIDGE for the node->guest SSH hop
QGA_GUEST_STAGE = "/root/qga-stage"
QGA_BOOT_BUDGET_S = 420
QGA_PKG_BRANCH = os.environ.get("TEZ_QGA_PKG_BRANCH", "")   # default: the guest's own ABI
QGA_STAGE_DIR = Path(os.environ.get("TEZ_QGA_STAGE_DIR", "/tmp/pfsense-qga-pkgs"))
FREEBSD_PKG_BASE = "https://pkg.freebsd.org"


def node_host():
    return os.environ["TF_VAR_proxmox_endpoint"].split("//")[1].split(":")[0]


def _node_ssh(cmd, timeout=30):
    return subprocess.run(
        ["ssh", "-i", str(Path("proxmox").resolve()), "-o", "BatchMode=yes",
         "-o", "ConnectTimeout=10", f"root@{node_host()}", cmd],
        capture_output=True, text=True, timeout=timeout)


def _guest_argv(cmd):
    proxy = f"ssh -i {Path('proxmox').resolve()} -o BatchMode=yes -W %h:%p root@{node_host()}"
    return ["ssh", "-i", str(Path("proxmox").resolve()), "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ProxyCommand={proxy}", f"{FW_ADMIN}@{BOOTSTRAP_LAN}", cmd]


def _guest_ssh(cmd, timeout=90):
    return subprocess.run(_guest_argv(cmd), capture_output=True, text=True, timeout=timeout)


QGA_SHELLCMD = "/usr/local/etc/rc.d/qemu-guest-agent start"


def agent_clone():
    n = node()
    up = proxmox_api("POST", f"/nodes/{n}/qemu/{NEWID}/clone", data={
        "newid": QGA_VMID, "name": QGA_WORK_NAME, "full": 1,
        "description": "pfsense-provision + qemu-guest-agent (build-pfsense-provision-template.py)"})
    wait_for_proxmox_task(n, up["data"])
    # agent=1 must exist BEFORE the first boot: it adds the virtio-serial guest-agent
    # channel the daemon (and our REST ping verify) need. net0/net1 both on the bridge:
    # vtnet1 is the template's LAN 192.168.1.1; vtnet0 (factory WAN, unconfigured) is inert.
    proxmox_api("PUT", f"/nodes/{n}/qemu/{QGA_VMID}/config", data={
        "agent": 1,
        "net0": f"virtio,bridge={QGA_BRIDGE}",
        "net1": f"virtio,bridge={QGA_BRIDGE}"})
    proxmox_api("POST", f"/nodes/{n}/qemu/{QGA_VMID}/status/start")
    print(f"cloned {NEWID} (pfsense-provision) -> {QGA_VMID} (agent=1, net0/net1 on "
          f"{QGA_BRIDGE}), started")


def _wait_guest_ssh():
    print(f"waiting for the guest's template SSH on {BOOTSTRAP_LAN} (through node {node_host()})")
    deadline = time.time() + QGA_BOOT_BUDGET_S
    while True:
        try:
            if _guest_ssh("echo ok", timeout=30).returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        if time.time() >= deadline:
            raise SystemExit(f"guest SSH never answered on {BOOTSTRAP_LAN} within "
                             f"{QGA_BOOT_BUDGET_S}s — is the scratch VM running with "
                             f"net1 on {QGA_BRIDGE} and the node alias in place?")
        time.sleep(10)


def _catalog_entries(catalog_yaml, names):
    """packagesite.yaml is JSON-lines: one package object per line."""
    found = {}
    with open(catalog_yaml) as fh:
        for line in fh:
            for want in names:
                if f'"name":"{want}"' in line:
                    obj = json.loads(line)
                    found[obj["name"]] = obj
                    break
    return found


def _resolve_packages(branch, want="qemu-guest-agent"):
    """Download packagesite for `branch` and walk `want`'s dependency closure; return
    [(pkg_name, repo_path)] in dependency order (deps first, `want` last)."""
    QGA_STAGE_DIR.mkdir(parents=True, exist_ok=True)
    meta = QGA_STAGE_DIR / "packagesite.pkg"
    if not meta.exists():
        subprocess.run(["curl", "-sSLf", "--globoff", "-o", str(meta),
                        f"{FREEBSD_PKG_BASE}/{branch}/latest/packagesite.pkg"], check=True)
    yaml = QGA_STAGE_DIR / "packagesite.yaml"
    if not yaml.exists():
        subprocess.run(["tar", "-xf", str(meta), "-C", str(QGA_STAGE_DIR)], check=True)

    objs, todo = {}, {want}
    while todo:
        found = _catalog_entries(yaml, todo)
        missing = todo - set(found)
        if missing:
            raise SystemExit(f"packagesite has no entry for: {sorted(missing)} (branch {branch})")
        todo = {dep for o in found.values() for dep in (o.get("deps") or {}) if dep not in objs}
        objs.update(found)

    order, done = [], set()
    while len(order) < len(objs):
        progressed = False
        for name, obj in objs.items():
            if name in done:
                continue
            if all(d in done for d in (obj.get("deps") or {})):
                order.append(name)
                done.add(name)
                progressed = True
        if not progressed:
            raise SystemExit(f"dependency cycle in the closure of {want}")
    return [(name, objs[name]["path"]) for name in order]


def _agent_ping(node_name, budget_s=60):
    """REST /agent/ping, retried — the success signal (NOT `qm guest ping`, which does
    not exist on this PVE version)."""
    deadline = time.time() + budget_s
    while True:
        try:
            proxmox_api("POST", f"/nodes/{node_name}/qemu/{QGA_VMID}/agent/ping")
            return True
        except Exception:                                # noqa: BLE001 - not ready yet
            if time.time() >= deadline:
                return False
            time.sleep(5)


def _install_script(packages):
    lines = ["#!/bin/sh", "set -u",
             # pfSense's 14-CURRENT kernel reports an older __FreeBSD_version than the
             # packages were built on; syscall ABI is stable within the 14 major and
             # the agent ping below is the functional proof.
             "export ASSUME_ALWAYS_YES=yes IGNORE_OSVERSION=yes",
             f"cd {QGA_GUEST_STAGE}", "FAIL=0",
             "installed() { pkg info -e \"$1\" >/dev/null 2>&1; }",
             "install_one() { name=\"$1\"; file=\"$2\"; "
             "if installed \"$name\"; then echo \"SKIP $name\"; "
             "else echo \"ADD $name ($file)\"; pkg add \"./$file\" || "
             "{ echo \"ADDFAIL $name\"; FAIL=1; }; fi; }"]
    for name, fname in packages:
        lines.append(f"install_one {shlex.quote(name)} {shlex.quote(fname)}")
    lines += ["[ \"$FAIL\" = 0 ] || { echo INSTALL_FAILED; exit 3; }",
              "installed qemu-guest-agent || exit 3",
              "ldd /usr/local/bin/qemu-ga 2>/dev/null | grep -q 'not found' "
              "&& { echo MISSING_LIBS; exit 4; }",
              "port=$(ls /dev/vtcon 2>/dev/null | grep org.qemu.guest_agent | head -1)",
              "[ -z \"$port\" ] && port=$(ls /dev/vtcon 2>/dev/null | head -1)",
              "[ -n \"$port\" ] || { echo NO_VTCON; exit 5; }",
              "printf 'qemu_guest_agent_enable=\"YES\"\\n' > /etc/rc.conf.local",
              "printf 'qemu_guest_agent_flags=\"-d -v -p /dev/vtcon/%s\"\\n' \"$port\" >> /etc/rc.conf.local",
              # pfSense boot never runs /usr/local/etc/rc.d/* — find_local_scripts_new
              # only globs *.sh — so the rc.conf enable alone is inert. The pfSense-
              # native hook is <system><afterbootupshellcmd> in config.xml (/etc/rc.bootup
              # mwexec's it at the end of bootup). Phase 5's generated configs carry the
              # same tag (firewall_ops.generate_team_config).
              "if ! grep -q afterbootupshellcmd /cf/conf/config.xml; then "
              f"sed -i '' -e 's,<system>,<system><afterbootupshellcmd>{QGA_SHELLCMD}"
              "</afterbootupshellcmd>,' /cf/conf/config.xml; fi",
              "rm -f /tmp/config.cache",
              "echo '--- rc.conf.local:'; cat /etc/rc.conf.local",
              "grep -o '<afterbootupshellcmd>.*</afterbootupshellcmd>' /cf/conf/config.xml"]
    return "\n".join(lines) + "\n"


def _safe_guest_name(fname):
    """Strip the pkg hash suffix (`~2$<hash>.pkg`): `$` in the name gets eaten by the
    remote shell during the push regardless of local quoting, so the guest sees a
    hash-free name and the install script refers to the same one."""
    import re
    return re.sub(r"~2\$[A-Za-z0-9]+\.pkg$", ".pkg", fname)


def agent_install():
    n = node()
    _node_ssh(f"ip addr add {QGA_ALIAS_IP}/24 dev {QGA_BRIDGE} 2>/dev/null || true")
    _wait_guest_ssh()
    abi = _guest_ssh("pkg config abi").stdout.strip()
    if not abi.startswith("FreeBSD:"):
        raise SystemExit(f"guest 'pkg config abi' gave {abi!r} — is this really the "
                         "pfsense-provision clone?")
    branch = QGA_PKG_BRANCH or abi
    if QGA_PKG_BRANCH and QGA_PKG_BRANCH != abi:
        raise SystemExit(f"TEZ_QGA_PKG_BRANCH={QGA_PKG_BRANCH} != guest ABI {abi} — "
                         "a mismatched-ABI package can break the appliance; align them")
    print(f"guest ABI {abi}; resolving the dependency closure from {branch}")
    packages = _resolve_packages(branch)
    fnames = []
    for name, relpath in packages:
        fname = relpath.split("/")[-1]
        dest = QGA_STAGE_DIR / fname
        if not dest.exists():
            subprocess.run(["curl", "-sSLf", "--globoff", "-o", str(dest),
                            f"{FREEBSD_PKG_BASE}/{branch}/latest/{relpath}"], check=True)
        fnames.append(fname)
        print(f"  staged {name}: {fname} ({dest.stat().st_size} bytes)")

    _guest_ssh(f"rm -rf {QGA_GUEST_STAGE} && mkdir -p {QGA_GUEST_STAGE}")
    guest_names = [_safe_guest_name(f) for f in fnames]
    script = _install_script(list(zip([n for n, _ in packages], guest_names)))
    (QGA_STAGE_DIR / "install.sh").write_text(script)
    for fname, guest_fname in list(zip(fnames, guest_names)):
        with open(QGA_STAGE_DIR / fname, "rb") as fh:
            subprocess.run(_guest_argv(f"cat > {QGA_GUEST_STAGE}/{guest_fname}"), stdin=fh, check=True)
    with open(QGA_STAGE_DIR / "install.sh", "rb") as fh:
        subprocess.run(_guest_argv(f"cat > {QGA_GUEST_STAGE}/install.sh"), stdin=fh, check=True)

    print("installing (pkg add in dependency order, then rc.conf.local)")
    r = _guest_ssh(f"sh {QGA_GUEST_STAGE}/install.sh", timeout=600)
    print(r.stdout.strip())
    if r.returncode != 0:
        raise SystemExit(f"install failed rc={r.returncode}: {(r.stderr or '').strip()[:300]}")
    _guest_ssh(f"rm -rf {QGA_GUEST_STAGE}")

    # Boot-path proof: an in-session `service onestart` dies with the SSH session's
    # hangup (the rc script runs qemu-ga foreground under -d), so reboot and let
    # rc.conf.local bring the agent up the way every future clone will see it.
    print("rebooting the guest — rc.conf.local must start the agent at boot")
    try:
        _guest_ssh("nohup sh -c 'sleep 2; /sbin/reboot' >/dev/null 2>&1 &", timeout=15)
    except subprocess.TimeoutExpired:
        pass                                    # the session drops as the guest goes down
    if not _agent_ping(n, budget_s=QGA_BOOT_BUDGET_S):
        raise SystemExit("the guest agent never answered REST /agent/ping after the "
                         "reboot — check `qm terminal` (rc.conf.local flags, vtcon port)")
    _wait_guest_ssh()
    r = _guest_ssh("pgrep -x qemu-ga >/dev/null && echo QGA_PROCESS_UP")
    if "QGA_PROCESS_UP" not in (r.stdout or ""):
        raise SystemExit("REST ping answered but no qemu-ga process found in the guest")
    print(f"qemu-guest-agent live after reboot — REST ping + process both verified")


def _vm_status(n, vmid):
    return proxmox_api("GET", f"/nodes/{n}/qemu/{vmid}/status/current")["data"]["status"]


def _halt_and_wait(n):
    """halt via the guest SSH, retrying until PVE reports stopped: the halt call itself
    fails softly (rc=255 whether the session dropped because the halt FIRED or because
    sshd refused), so the state poll is the only truth."""
    for attempt in range(5):
        if _vm_status(n, QGA_VMID) == "stopped":
            return
        try:
            _guest_ssh("halt -p", timeout=30)
        except Exception:                            # noqa: BLE001 - session drop is fine
            pass
        for _ in range(12):                          # 60s per attempt
            time.sleep(5)
            if _vm_status(n, QGA_VMID) == "stopped":
                return
    raise SystemExit(f"vmid {QGA_VMID} did not power off after 5 halt attempts")


def agent_seal():
    n = node()
    _halt_and_wait(n)
    _node_ssh(f"ip addr del {QGA_ALIAS_IP}/24 dev {QGA_BRIDGE} 2>/dev/null || true")
    # Name is the resolution contract (deploy resolves templates by NAME); vmid is
    # convention. Swap names so `pfsense-provision` points at the retrofit, keep the
    # old provisioning template as a stopgap rollback. NEWID (957), NOT SOURCE_VMID —
    # the rollback must keep SSH + the deploy key.
    for vmid, name in ((NEWID, QGA_BACKUP_NAME), (QGA_VMID, NAME)):
        r = _node_ssh(f"qm set {vmid} --name {name}", timeout=30)
        if r.returncode != 0:
            raise SystemExit(f"qm set {vmid} --name {name} failed: {r.stderr.strip()[:200]}")
    proxmox_api("PUT", f"/nodes/{n}/qemu/{QGA_VMID}/config", data={"tags": "template"})
    up = proxmox_api("POST", f"/nodes/{n}/qemu/{QGA_VMID}/template")
    wait_for_proxmox_task(n, up["data"])
    cfg = proxmox_api("GET", f"/nodes/{n}/qemu/{QGA_VMID}/config")["data"]
    if not any("base-" in str(v) for k, v in cfg.items() if k.startswith(("ide", "scsi", "sata", "virtio"))):
        raise SystemExit("conversion did not produce base- volumes — linked clones would fail")
    print(f"{QGA_VMID} sealed as template 'pfsense-provision' (qemu-guest-agent inside); "
          f"rollback = vmid {NEWID} '{QGA_BACKUP_NAME}' (swap the names back, destroy {QGA_VMID})")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "shot":
        print(console_screenshot(NEWID, sys.argv[2]))
    elif cmd in ("clone", "edit", "seal"):
        globals()[cmd]()
    elif cmd in ("agent-clone", "agent-install", "agent-seal"):
        globals()[cmd.replace("-", "_")]()
    else:
        raise SystemExit(__doc__)
