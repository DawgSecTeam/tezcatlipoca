"""Per-competition console-only Proxmox tokens for the student portal (docs/portal.md).

The portal on the engine opens team consoles through PVE's vncproxy/vncwebsocket, which needs a
Proxmox credential ON the engine — a host every team NIC touches. So that credential is minted
per competition and scoped to the bone:

  user   tezcon-<comp>-<run_id>@pve       one per node the competition's teams live on
  token  <user>!portal                    privsep=0: the token IS the user's permissions
  role   TezConsole = VM.Console only     (shared across competitions; never deleted)
  ACLs   /vms/<vmid> -> user, one per team VM of THIS competition on that node

What the token can do is exactly what the students already get: a console on this competition's
own team VMs. It cannot start/stop, read configs, or touch any other VM.

Teardown deletes exactly `tezcon-<comp>-<run_id>@pve` on every node (the user's token and ACLs
go with it). Exact name only — never a prefix sweep: AGENTS.md records an over-broad sweep
destroying two other competitions' infrastructure. A user that is already gone is success.

This module never decides WHETHER the portal runs; portal_ops calls it and degrades (logins
work, consoles off) when minting fails.
"""

import os

import requests

from infra.client import proxmox_api_for, proxmox_request
from estate import config

ROLE_ID = "TezConsole"
ROLE_PRIVS = "VM.Console"
TOKEN_NAME = "portal"
# Privileges the deploy token needs to mint (preflight probe). Sys.Modify only matters the
# first time, when the TezConsole role does not exist yet on a node.
REQUIRED_PRIVS = ("User.Modify", "Permissions.Modify", "Realm.AllocateUser", "Sys.Modify")


def console_user(comp_name, run_id):
    """The one PVE userid this run's portal owns (deterministic: teardown recomputes it)."""
    if not comp_name or not run_id:
        raise ValueError("console user needs both a competition name and a run id")
    return f"tezcon-{comp_name}-{run_id}@pve"


def _http_detail(e):
    resp = getattr(e, "response", None)
    if resp is None:
        return str(e)
    return f"HTTP {resp.status_code}: {(resp.text or '')[:200]}"


def vncproxy_mint(endpoint, token, pve_node, vmid, fingerprint=None, timeout=30):
    """Mint a PVE noVNC ticket: POST /nodes/<pve_node>/qemu/<vmid>/vncproxy {websocket: 1}.

    Returns {port: int, ticket: str}. The one handshake every console consumer shares
    (portal broker, the two build-*-provision-template tools). `fingerprint` pins the TLS
    peer for a per-node record; None leaves the env rules of proxmox_request in force."""
    url = (f"{endpoint.rstrip('/')}/api2/json/nodes/{pve_node}/qemu/{int(vmid)}/vncproxy")
    r = proxmox_request("POST", url, data={"websocket": 1},
                        headers={"Authorization": f"PVEAPIToken={token}"},
                        fingerprint=fingerprint, timeout=timeout)
    r.raise_for_status()
    data = r.json().get("data") or {}
    if not data.get("port") or not data.get("ticket"):
        raise ValueError("Proxmox returned no console port/ticket")
    return {"port": int(data["port"]), "ticket": str(data["ticket"])}


def vnc_screenshot(endpoint, token, pve_node, vmid, out_path, fingerprint=None):
    """PNG of a VM's VGA console via the PVE vncwebsocket (raw RFB, 3.3-style handshake).

    PVE rejects the 3.8 type-list path over vncwebsocket, and packs several RFB messages
    into one websocket frame, so reads are buffered. Heavy imports (websocket-client, PIL,
    cryptography) stay inside: the tools are the only callers, and they shell out."""
    import ssl
    import struct
    from urllib.parse import quote

    import websocket
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from PIL import Image

    d = vncproxy_mint(endpoint, token, pve_node, vmid, fingerprint=fingerprint)
    host = endpoint.split("//")[1].split(":")[0]
    ws = websocket.create_connection(
        f"wss://{host}:8006/api2/json/nodes/{pve_node}/qemu/{int(vmid)}/vncwebsocket"
        f"?port={d['port']}&vncticket={quote(d['ticket'], safe='')}",
        header={"Authorization": f"PVEAPIToken={token}"},
        sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=60)
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


def _is_absent(e):
    """PVE answers 500 for "no such user/role/token" — tell that apart from real failures."""
    resp = getattr(e, "response", None)
    text = (resp.text or "").lower() if resp is not None else ""
    return resp is not None and resp.status_code in (404, 500) and (
        "does not exist" in text or "no such" in text)


def _exists_already(e):
    resp = getattr(e, "response", None)
    text = (resp.text or "").lower() if resp is not None else ""
    return resp is not None and "already exists" in text


def node_targets(comp_dir_targets, placement, default_node):
    """Group a competition's team VMs by the node record that hosts them.

    Returns {node_key: {"pve_node", "endpoint", "token_env", "tls_fingerprint", "vmids"}}.
    node_key is the nodes.json record NAME under a multi-node placement (PVE hostnames can
    repeat across independent hosts) and the env node otherwise; every portal box record
    carries the same key, so the portal can find its node without re-deriving placement.
    """
    out = {}
    if placement:
        for t in comp_dir_targets:
            key = placement["team_nodes"][t["team_key"]]
            rec = placement["nodes"][key]
            entry = out.setdefault(key, {
                "pve_node": rec["node"], "endpoint": rec["endpoint"],
                "token_env": rec["token_env"],
                "tls_fingerprint": rec.get("tls_fingerprint") or "", "vmids": []})
            entry["vmids"].append(int(t["vmid"]))
    else:
        entry = {"pve_node": default_node,
                 "endpoint": config.active().proxmox.endpoint,
                 "token_env": "TF_VAR_proxmox_api_token",
                 "tls_fingerprint": config.active().proxmox.tls_fingerprint,
                 "vmids": [int(t["vmid"]) for t in comp_dir_targets]}
        out[default_node] = entry
    for entry in out.values():
        entry["vmids"] = sorted(set(entry["vmids"]))
    return out


def team_node_key(target, placement, default_node):
    """The node_key (see node_targets) of one target."""
    return placement["team_nodes"][target["team_key"]] if placement else default_node


def probe_permissions(endpoint, token, api=proxmox_api_for):
    """Missing REQUIRED_PRIVS of the deploy token on `/`, as a list (empty = can mint)."""
    perms = api(endpoint, token, "GET", "/access/permissions", params={"path": "/"})["data"]
    have = perms.get("/") or {}
    return [p for p in REQUIRED_PRIVS if not have.get(p)]


def _ensure_role(endpoint, token, api):
    try:
        api(endpoint, token, "GET", f"/access/roles/{ROLE_ID}")
        return
    except requests.HTTPError as e:
        if not _is_absent(e) and getattr(e.response, "status_code", None) != 500:
            raise
    try:
        api(endpoint, token, "POST", "/access/roles", data={"roleid": ROLE_ID,
                                                           "privs": ROLE_PRIVS})
    except requests.HTTPError as e:
        if not _exists_already(e):  # another deploy created it between our GET and POST
            raise


def mint_node_token(comp_name, run_id, node, token, previous=None, api=proxmox_api_for):
    """Mint (or re-mint) this run's console token on one node and (re)apply its ACLs.

    `previous` is the {user, token_id, secret} recorded by an earlier attempt of the SAME run:
    the secret is reused (PVE only reveals it at creation) as long as the token still exists;
    otherwise the token is recreated. ACL PUTs are idempotent, so a resume re-applies them —
    which also picks up vmids added since (a redeploy that rebuilt boxes)."""
    endpoint = node["endpoint"]
    userid = console_user(comp_name, run_id)
    _ensure_role(endpoint, token, api)
    try:
        api(endpoint, token, "POST", "/access/users",
            data={"userid": userid,
                  "comment": f"tezcatlipoca portal console ({comp_name}, {run_id})"})
    except requests.HTTPError as e:
        if not _exists_already(e):
            raise
    token_path = f"/access/users/{userid}/token/{TOKEN_NAME}"
    full_id = f"{userid}!{TOKEN_NAME}"
    secret = None
    if previous and previous.get("user") == userid and previous.get("secret"):
        try:
            api(endpoint, token, "GET", token_path)
            secret = previous["secret"]
        except requests.HTTPError as e:
            if not _is_absent(e):
                raise
    if secret is None:
        try:
            api(endpoint, token, "DELETE", token_path)
        except requests.HTTPError as e:
            if not _is_absent(e):
                raise
        made = api(endpoint, token, "POST", token_path,
                   data={"privsep": 0, "comment": "student portal console relay"})["data"]
        secret = made["value"]
        full_id = made.get("full-tokenid") or full_id
    for vmid in node["vmids"]:
        api(endpoint, token, "PUT", "/access/acl",
            data={"path": f"/vms/{vmid}", "roles": ROLE_ID, "users": userid, "propagate": 0})
    return {"user": userid, "token_id": full_id, "secret": secret}


def mint_console_tokens(comp_name, run_id, nodes, previous=None, api=proxmox_api_for,
                        env=os.environ):
    """Mint the console token on every node of `nodes` (node_targets output).

    Returns (tokens, problems): tokens maps node_key -> {user, token_id, secret} for the nodes
    that succeeded; problems lists "<node_key>: <why>" for the rest. Never raises for a
    Proxmox refusal — the caller records a degradation and the portal runs without consoles on
    those nodes."""
    tokens, problems = {}, []
    previous = previous or {}
    for key, node in nodes.items():
        deploy_token = env.get(node["token_env"])
        if not deploy_token or not node.get("endpoint"):
            problems.append(f"{key}: no endpoint/token in the environment ({node['token_env']})")
            continue
        try:
            missing = probe_permissions(node["endpoint"], deploy_token, api=api)
            if missing:
                problems.append(f"{key}: deploy token lacks {', '.join(missing)}")
                continue
            tokens[key] = mint_node_token(comp_name, run_id, node, deploy_token,
                                          previous=previous.get(key), api=api)
        except (requests.RequestException, KeyError, ValueError) as e:
            problems.append(f"{key}: {_http_detail(e)}")
    return tokens, problems


def revoke_console_tokens(comp_name, run_id, nodes, api=proxmox_api_for, env=os.environ):
    """Delete exactly this run's console user on every node. Returns a list of problems
    (empty = clean). An already-absent user counts as clean, so teardown re-runs converge."""
    userid = console_user(comp_name, run_id)
    problems = []
    for key, node in nodes.items():
        deploy_token = env.get(node["token_env"])
        if not deploy_token or not node.get("endpoint"):
            problems.append(f"{key}: no endpoint/token in the environment ({node['token_env']})")
            continue
        try:
            api(node["endpoint"], deploy_token, "DELETE", f"/access/users/{userid}")
            print(f"  Portal console user {userid} removed on {key}")
        except requests.HTTPError as e:
            if _is_absent(e):
                print(f"  Portal console user {userid} already absent on {key}")
            else:
                problems.append(f"{key}: {_http_detail(e)}")
        except requests.RequestException as e:
            problems.append(f"{key}: {e}")
    return problems
