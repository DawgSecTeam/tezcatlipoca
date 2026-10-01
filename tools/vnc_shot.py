#!/usr/bin/env python3
"""Capture a VNC framebuffer screenshot of a VM via the PVE vncwebsocket API.

Usage: vnc_shot.py <vmid> <out.png>
Auth: TF_VAR_proxmox_endpoint + TF_VAR_proxmox_api_token from .env (cwd).
RFB flow: raw-RFB-over-websocket -> security types -> VNC auth with the
vncproxy ticket as password (DES key = bit-reversed password bytes) ->
full raw framebuffer -> PNG."""

import os
import ssl
import struct
import sys

import requests
import urllib3
import websocket
from urllib.parse import quote
from PIL import Image
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

urllib3.disable_warnings()


def rfb_des_key(password: bytes) -> bytes:
    """VNC auth DES key: first 8 password bytes, each bit-reversed."""
    key = password[:8].ljust(8, b"\x00")
    return bytes(int(f"{b:08b}"[::-1], 2) for b in key)


def des_challenge_response(password: bytes, challenge: bytes) -> bytes:
    cipher = Cipher(algorithms.TripleDES(rfb_des_key(password)), modes.ECB())
    enc = cipher.encryptor()
    return enc.update(challenge) + enc.finalize()


def main(vmid, out_path):
    sys.path.insert(0, ".")
    from dotenv import load_dotenv
    load_dotenv(".env")
    endpoint = os.environ["TF_VAR_proxmox_endpoint"].rstrip("/")
    host = endpoint.split("//")[1].split(":")[0]
    auth = {"Authorization": f"PVEAPIToken={os.environ['TF_VAR_proxmox_api_token']}"}
    s = requests.Session(); s.headers.update(auth); s.verify = False
    d = s.post(f"{endpoint}/api2/json/nodes/pve/qemu/{vmid}/vncproxy",
               data={"websocket": 1}, timeout=30).json()["data"]
    ws = websocket.create_connection(
        f"wss://{host}:8006/api2/json/nodes/pve/qemu/{vmid}/vncwebsocket"
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

    rx(12)  # RFB 003.008
    ws.send_binary(b"RFB 003.003\n")  # PVE/qemu answers 3.3-style after a 3.3 reply
    sectype = struct.unpack(">I", rx(4))[0]
    if sectype == 2:  # VNC auth with the proxy ticket as password
        challenge = rx(16)
        ws.send_binary(des_challenge_response(d["ticket"].encode(), challenge))
        result = struct.unpack(">I", rx(4))[0]
        if result != 0:
            raise SystemExit(f"VNC auth failed (security result {result})")
    elif sectype != 1:
        raise SystemExit(f"unsupported security type {sectype}")
    ws.send_binary(b"\x01")  # client init (shared)
    init = rx(24)
    w, h = struct.unpack(">HH", init[:4])
    pf = bytes([0, 0, 0, 0, 32, 24, 0, 0,
                0, 0, 0, 0,
                255, 255, 255,
                0, 0, 0, 0, 0, 0, 0, 0, 0])
    ws.send_binary(b"\x00" + pf)
    ws.send_binary(b"\x02" + b"\x00" + struct.pack(">H", 1) + struct.pack(">i", 0))
    ws.send_binary(b"\x03" + b"\x00" + struct.pack(">HHHH", 0, 0, w, h))
    hdr = rx(16)
    blen = struct.unpack(">I", hdr[12:16])[0]
    px = rx(blen)
    img = Image.frombytes("RGBX", (w, h), px).convert("RGB")
    img.save(out_path)
    print("saved", out_path, img.size)
    ws.close()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
