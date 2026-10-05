#!/usr/bin/env python3
"""Build the provisioning-friendly pfSense template (`pfsense-provision`) from the stock
`pfsense` template, ONCE per node.

The stock template (956) boots to a VGA menu with SSH off and no LAN address, which is why
phase 5 used to type a fetch command at the console. This builder types the equivalent
edits ONE time, so every later deploy is `wait for SSH, push config`:

  * SSH enabled, the deploy public key in admin's authorizedkeys (admin is uid 0)
  * LAN = vtnet1 192.168.1.1/24 (the bootstrap address; phase 5 replaces the whole config)
  * serial console enabled (`<enableserial/>`; the VM gets serial0 so `qm terminal` works)

Usage (from a worktree root with .env loaded):
  tools/build-pfsense-provision-template.py clone     # full-clone 956 -> NEWID, start it
  tools/build-pfsense-provision-template.py edit      # type the edits at the console
  tools/build-pfsense-provision-template.py shot OUT  # console PNG (verify by eye)
  tools/build-pfsense-provision-template.py seal      # halt, serial0, convert to template
"""

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv(".env")

from pve_api import proxmox_api, wait_for_proxmox_task  # noqa: E402

SOURCE_VMID = 956
NEWID = int(os.environ.get("PFSENSE_PROVISION_VMID", "957"))
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


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "shot":
        print(console_screenshot(NEWID, sys.argv[2]))
    elif cmd in ("clone", "edit", "seal"):
        globals()[cmd]()
    else:
        raise SystemExit(__doc__)
