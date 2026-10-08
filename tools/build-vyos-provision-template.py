#!/usr/bin/env python3
"""Build the provisioning-friendly VyOS template (`vyos-provision`) from the official
VyOS stream ISO, ONCE per node. The pfSense twin is tools/build-pfsense-provision-template.py;
the contract phase 5 depends on is the same:

  * SSH service on, the deploy public key (proxmox.pub) in vyos's authorized_keys
  * LAN eth1 = 192.168.1.1/24 (the bootstrap address; phase 5 replaces it per team)
  * a getty on ttyS0 (VM gets serial0, so `qm terminal` gives a recovery login)

Usage (from a worktree root with the TARGET NODE's env exported — for .150
`set -a; source .env.cyberrange-20260930; set +a` first):

  tools/build-vyos-provision-template.py download   # node-side curl of the stream ISO
  tools/build-vyos-provision-template.py create     # scratch VM: ISO + 8G disk, both NICs on the bridge
  tools/build-vyos-provision-template.py install    # console-typed `install image` walk, reboot into disk
  tools/build-vyos-provision-template.py provision  # console-typed: eth1 bootstrap addr, ssh, serial, deploy key
  tools/build-vyos-provision-template.py check      # LIVE-validate the real generated phase-5 scripts, then undo
  tools/build-vyos-provision-template.py seal       # poweroff, serial0, convert to template
  tools/build-vyos-provision-template.py shot OUT   # console PNG (verify by eye between steps)
  tools/build-vyos-provision-template.py type TEXT  # type one line at the console (live recovery)

The `install` walk is the fragile part — the installer's prompt sequence is
version-specific. Each typed line is deliberate; when a prompt differs, take a `shot`,
then drive the rest with `type`. Env knobs:
  TEZ_VYOS_VMID       scratch/template vmid (default 959)
  TEZ_VYOS_ISO_URL    stream ISO URL   (default 2026.03 generic amd64)
  TEZ_VYOS_ISO_DIR    node ISO dir     (default /var/lib/vz/template/iso)
  TEZ_VYOS_STORAGE    disk storage     (default hdd)
  TEZ_VYOS_BRIDGE     NIC bridge       (default vmbr0)
  TEZ_VYOS_PW         the vyos admin password set at install and typed at login
"""

import base64
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv(".env")

from firewall_ops import generate_vyos_commands, generate_vyos_lan_commands  # noqa: E402
from pve_api import proxmox_api, wait_for_proxmox_task  # noqa: E402

VMID = int(os.environ.get("TEZ_VYOS_VMID", "959"))
NAME = "vyos-provision"
ISO_URL = os.environ.get(
    "TEZ_VYOS_ISO_URL",
    "https://community-downloads.vyos.dev/stream/2026.03/vyos-2026.03-generic-amd64.iso")
ISO_NAME = ISO_URL.rsplit("/", 1)[-1]
ISO_DIR = os.environ.get("TEZ_VYOS_ISO_DIR", "/var/lib/vz/template/iso")
STORAGE = os.environ.get("TEZ_VYOS_STORAGE", "hdd")
BRIDGE = os.environ.get("TEZ_VYOS_BRIDGE", "vmbr0")
PW = os.environ.get("TEZ_VYOS_PW", "tez-vyos-build")
BOOTSTRAP_LAN = "192.168.1.1"
PUBKEY_FILE = Path("proxmox.pub")
# A throwaway team id for `check` (no collision with anything real; undone before seal)
CHECK_TID = "200"
CHECK_DNAT = ["4470->192.168.200.9"]

_KEYS = {".": "dot", "/": "slash", "-": "minus", "_": "shift-minus", ",": "comma",
         "=": "equal", " ": "spc", ":": "shift-semicolon", "@": "shift-2",
         "|": "shift-backslash", "<": "shift-comma", ">": "shift-dot", "'": "apostrophe",
         '"': "shift-apostrophe", "+": "shift-equal", ";": "semicolon", "&": "shift-7",
         "(": "shift-9", ")": "shift-0", "#": "shift-3", "!": "shift-1", "*": "shift-8"}


def node():
    return os.environ["TF_VAR_proxmox_node"]


def _monitor(command):
    return proxmox_api("POST", f"/nodes/{node()}/qemu/{VMID}/monitor",
                       data={"command": command})


def type_line(text, inter_key_s=0.04, after_s=0.8):
    for ch in text:
        if ch.isdigit() or "a" <= ch <= "z":
            key = ch
        elif "A" <= ch <= "Z":
            key = f"shift-{ch.lower()}"
        elif ch in _KEYS:
            key = _KEYS[ch]
        else:
            raise ValueError(f"no sendkey mapping for {ch!r}")
        _monitor(f"sendkey {key}")
        time.sleep(inter_key_s)
    _monitor("sendkey ret")
    time.sleep(after_s)


def console_screenshot(out_path):
    """PNG of the VM's VGA console via the PVE vncwebsocket (raw RFB, 3.3-style handshake).
    Copied from build-pfsense-provision-template.py (tools stay self-contained)."""
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
    d = sess.post(f"{endpoint}/api2/json/nodes/{node()}/qemu/{VMID}/vncproxy",
                  data={"websocket": 1}, timeout=30).json()["data"]
    ws = websocket.create_connection(
        f"wss://{host}:8006/api2/json/nodes/{node()}/qemu/{VMID}/vncwebsocket"
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


def _node_ssh(cmd, timeout=30):
    node_host = os.environ["TF_VAR_proxmox_endpoint"].split("//")[1].split(":")[0]
    return subprocess.run(
        ["ssh", "-i", str(Path("proxmox").resolve()), "-o", "BatchMode=yes",
         "-o", "ConnectTimeout=10", f"root@{node_host}", cmd],
        capture_output=True, text=True, timeout=timeout)


def _guest_argv(cmd=None):
    """SSH argv to the guest through the node (node-side alias on BRIDGE). With cmd:
    one-shot remote command. Without: reads the CLI session from stdin (the VyOS login
    shell IS the CLI; piped lines execute exactly as console-typed)."""
    proxy = f"ssh -i {Path('proxmox').resolve()} -o BatchMode=yes -W %h:%p root@{node_host()}"
    args = ["ssh", "-i", str(Path("proxmox").resolve()), "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ProxyCommand={proxy}", f"vyos@{BOOTSTRAP_LAN}"]
    if cmd:
        args.append(cmd)
    return args


def _guest_ssh(cmd, timeout=90, script=None):
    return subprocess.run(_guest_argv(cmd), input=script,
                          capture_output=True, text=True, timeout=timeout)


def node_host():
    return os.environ["TF_VAR_proxmox_endpoint"].split("//")[1].split(":")[0]


def wait_status(want, budget_s, what):
    deadline = time.time() + budget_s
    while True:
        st = proxmox_api("GET", f"/nodes/{node()}/qemu/{VMID}/status/current")["data"]["status"]
        if st == want:
            return
        if time.time() >= deadline:
            raise SystemExit(f"vmid {VMID} never reached status '{want}' ({what})")
        time.sleep(5)


def download():
    iso_path = f"{ISO_DIR}/{ISO_NAME}"
    r = _node_ssh(f"test -s {iso_path} && echo PRESENT || echo MISSING", timeout=15)
    if "PRESENT" in r.stdout:
        print(f"{iso_path} already on the node")
        return
    print(f"downloading {ISO_URL} → {node_host()}:{iso_path} (node-side curl)")
    r = _node_ssh(f"curl -fL --retry 3 -o {iso_path}.part {ISO_URL} "
                  f"&& mv {iso_path}.part {iso_path} && ls -la {iso_path}", timeout=900)
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode != 0:
        raise SystemExit(f"download failed: {r.stderr.strip()[:300]}")


def create():
    n = node()
    iso_path = f"{ISO_DIR}/{ISO_NAME}"
    if _node_ssh(f"test -s {iso_path} && echo PRESENT", timeout=15).returncode != 0:
        raise SystemExit(f"{iso_path} missing on the node — run `download` first")
    proxmox_api("POST", f"/nodes/{n}/qemu", data={
        "vmid": VMID, "name": NAME, "cores": 1, "memory": 1024, "ostype": "l26",
        "scsi0": f"{STORAGE}:8", "scsihw": "virtio-scsi-pci",
        "ide2": f"local:iso/{ISO_NAME},media=cdrom",
        "net0": f"virtio,bridge={BRIDGE}", "net1": f"virtio,bridge={BRIDGE}",
        "boot": "order=ide2;scsi0", "serial0": "socket",
        "agent": 0,
        "description": "vyos-provision build scratch (build-vyos-provision-template.py)"})
    proxmox_api("POST", f"/nodes/{n}/qemu/{VMID}/status/start")
    print(f"vmid {VMID} created ({STORAGE} 8G, ide2={ISO_NAME}, net0/net1 on {BRIDGE}, "
          f"serial0 socket) and started — wait ~90s for the live-boot login, then `shot`")


def install():
    """Console-typed `install image`. The live ISO autologins as vyos on tty1; the prompt
    sequence below matches the 2026.03 stream. VERIFY each stage with `shot` — when a
    prompt differs, finish the walk with `type`."""
    print("typing the installer walk (assumes the live-boot login prompt, ~90s after start)")
    type_line("", after_s=2)                 # wake the console / clear a half line
    type_line("install image", after_s=4)
    type_line("Yes", after_s=3)              # Would you like to continue? [No]
    type_line("", after_s=3)                 # image name: accept the default
    type_line(PW, after_s=2)                 # password for user 'vyos'
    type_line(PW, after_s=2)                 # confirm
    type_line("", after_s=3)                 # default console [KVM]
    type_line("", after_s=3)                 # continue prompts if any
    print("typed; the squashfs copy takes minutes — `shot` until you see "
          "`Setup complete`, then run `reboot-into-disk`")
    type_line("")                            # in case a final [Yes]-style confirm remains


def reboot_into_disk():
    """The installer left the ISO attached; detach it BEFORE rebooting or the live ISO
    boots again (boot order is ide2 first)."""
    n = node()
    print("detaching the ISO, then rebooting into the installed disk")
    proxmox_api("PUT", f"/nodes/{n}/qemu/{VMID}/config", data={"delete": "ide2"})
    type_line("reboot", after_s=2)
    print("rebooting — ~60-90s to the installed system's login prompt")


def provision():
    """Console-typed bootstrap contract: eth1 = 192.168.1.1/24, SSH on, serial getty,
    deploy key in vyos's authorized_keys. Assumes the installed system's login prompt."""
    key_b64 = base64.b64encode(PUBKEY_FILE.read_bytes().strip() + b"\n").decode()
    print("logging in and typing the provision config")
    type_line("vyos", after_s=2)             # login:
    type_line(PW, after_s=3)                 # Password:
    type_line("configure", after_s=2)
    type_line(f"set interfaces ethernet eth1 address {BOOTSTRAP_LAN}/24", after_s=1)
    type_line("set service ssh port 22", after_s=1)
    type_line("set system console device ttyS0 speed 115200", after_s=1)
    type_line("set system host-name vyos-provision", after_s=1)
    type_line("commit", after_s=4)
    type_line("save", after_s=4)
    type_line("exit", after_s=2)             # back to op mode
    print("installing the deploy key (b64 → authorized_keys)")
    type_line("mkdir -p ~/.ssh", after_s=1)
    type_line(f"echo {key_b64} | base64 -d >> ~/.ssh/authorized_keys", after_s=1)
    type_line("chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys", after_s=1)
    print("typed; verify with `shot`, then `check-key` (needs the node-side alias)")


def alias(add=True):
    verb = "add" if add else "del"
    r = _node_ssh(f"ip addr {verb} 192.168.1.2/24 dev {BRIDGE} 2>/dev/null; echo done",
                  timeout=15)
    if r.returncode != 0:
        raise SystemExit(f"alias {verb} failed: {r.stderr.strip()[:200]}")


def check_key():
    """Proof the deploy's SSH path works: alias on the node bridge, then the deploy key
    answers on the bootstrap address."""
    alias(add=True)
    try:
        r = subprocess.run(
            ["ssh", "-i", str(Path("proxmox").resolve()), "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             f"-o", f"ProxyCommand=ssh -i {Path('proxmox').resolve()} -o BatchMode=yes "
                    f"-W %h:%p root@{node_host()}",
             f"vyos@{BOOTSTRAP_LAN}", "show version | grep Version"],
            capture_output=True, text=True, timeout=60)
        print(r.stdout.strip() or r.stderr.strip())
        if r.returncode != 0:
            raise SystemExit(f"deploy-key SSH failed rc={r.returncode}: "
                             f"{(r.stderr or '').strip()[:300]}")
    finally:
        alias(add=False)


def check():
    """LIVE-validate the REAL generated phase-5 scripts against this image BEFORE it is
    sealed: push the pass-A script (throwaway team id, DNAT path exercised), require the
    commit canary, confirm eth naming, then delete everything pass A created so the
    sealed template carries only the bootstrap contract. Pass B is NOT run here — it
    moves eth1 off the bootstrap address; its syntax is a subset of pass A's."""
    alias(add=True)
    try:
        eth = _guest_ssh("show interfaces | grep -E '^[A-Z]'", timeout=60).stdout
        for nic in ("eth0", "eth1"):
            if nic not in eth:
                raise SystemExit(f"no {nic} in `show interfaces` — NIC naming differs "
                                 f"from the terraform net0/net1 WAN/LAN order:\n{eth}")
        print("eth0/eth1 present (terraform NIC order holds)")

        pass_a = generate_vyos_commands(CHECK_TID, CHECK_DNAT)
        out = _guest_ssh(script=pass_a, timeout=120).stdout
        if "commit complete" not in out or f"fw-team{CHECK_TID}" not in out:
            raise SystemExit("generated pass-A script FAILED against this image:\n"
                             + out[-2000:])
        print("pass-A script committed clean (syntax + canary verified live)")

        undo = "\n".join([
            "configure",
            f"delete interfaces ethernet eth0 address 172.31.{CHECK_TID}.2/30",
            "delete protocols static route 0.0.0.0/0",
            "delete firewall",
            "delete nat",
            "delete system host-name",
            "commit",
            "save",
            "exit",
        ]) + "\n"
        out = _guest_ssh(script=undo, timeout=120).stdout
        if "commit complete" not in out:
            raise SystemExit(f"check cleanup failed:\n{out[-2000:]}")
        eth1 = _guest_ssh(f"show interfaces ethernet eth1 | grep {BOOTSTRAP_LAN}",
                          timeout=30).stdout
        if BOOTSTRAP_LAN not in eth1:
            raise SystemExit(f"eth1 lost its bootstrap address during check:\n{eth1}")
        print(f"cleanup committed; eth1 still {BOOTSTRAP_LAN} — template state intact")
    finally:
        alias(add=False)


def seal():
    n = node()
    _guest_ssh("poweroff", timeout=30)
    wait_status("stopped", 300, "poweroff after provisioning")
    proxmox_api("PUT", f"/nodes/{n}/qemu/{VMID}/config", data={"tags": "template"})
    up = proxmox_api("POST", f"/nodes/{n}/qemu/{VMID}/template")
    wait_for_proxmox_task(n, up["data"])
    cfg = proxmox_api("GET", f"/nodes/{n}/qemu/{VMID}/config")["data"]
    if not any("base-" in str(v) for k, v in cfg.items()
               if k.startswith(("ide", "scsi", "sata", "virtio"))):
        raise SystemExit("conversion did not produce base- volumes — linked clones would fail")
    print(f"{VMID} sealed as template '{NAME}' — resolve by NAME; "
          f"satellites need sync-template.py or a rebuild")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "shot":
        print(console_screenshot(sys.argv[2]))
    elif cmd == "type":
        type_line(sys.argv[2])
        print("typed")
    elif cmd in ("download", "create", "install", "provision", "check", "seal"):
        globals()[cmd]()
    elif cmd == "reboot-into-disk":
        reboot_into_disk()
    elif cmd == "check-key":
        check_key()
    else:
        raise SystemExit(__doc__)
