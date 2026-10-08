"""Proxmox web console: mint a VNC ticket per click and relay the websocket.

Flow (one console open):
  1. mint(box)   — POST /nodes/<node>/qemu/<vmid>/vncproxy {websocket: 1} on the box's own
                   Proxmox host, with this competition's console-only token. The {port, ticket}
                   pair is parked under a random one-shot relay id (RELAY_TTL seconds).
  2. take(id)    — the browser's noVNC opens /console/ws/<id>; the id is popped (single use).
  3. relay(...)  — open wss://<host>:8006/.../vncwebsocket?port&vncticket with the token header
                   and pipe frames both ways until either side closes.

The token never leaves the portal: the browser holds only the relay id and the VNC ticket, which
noVNC uses as the RFB password and which is useless without the relay. The same wire shape is
already proven live by tools/build-pfsense-provision-template.py console_screenshot (including
PVE's quirk that the websocket only completes an RFB 3.3 handshake — the console page caps
noVNC at 3.3 for that reason).

TLS: Proxmox hosts carry self-signed certs. With a recorded sha256 fingerprint the peer cert is
pinned (both the vncproxy POST and the websocket); without one the lab default applies — the
same rule pve_api.proxmox_request uses on the deploy side.
"""

import asyncio
import hashlib
import http.client
import json
import secrets
import ssl
import threading
import time
from urllib.parse import quote, urlencode, urlparse

RELAY_TTL = 30  # seconds between mint and the browser's websocket open


class ConsoleError(RuntimeError):
    """A console could not be opened (Proxmox refused or was unreachable)."""


def _norm_fp(fp):
    return (fp or "").replace(":", "").strip().lower()


def _ssl_context():
    # Verification is by fingerprint (below) or not at all — never by a CA these hosts lack.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _check_fingerprint(der_cert, want):
    want = _norm_fp(want)
    if not want:
        return
    got = hashlib.sha256(der_cert or b"").hexdigest()
    if got != want:
        raise ConsoleError(f"Proxmox TLS fingerprint mismatch (got {got[:16]}…)")


def node_host(node):
    """(host, port) of a node record's endpoint."""
    u = urlparse(node["endpoint"])
    return u.hostname, u.port or 8006


def vncproxy(node, vmid, timeout=20):
    """POST vncproxy on the node; returns {port, ticket}. Blocking — call via to_thread."""
    host, port = node_host(node)
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=_ssl_context())
    try:
        conn.connect()
        _check_fingerprint(conn.sock.getpeercert(binary_form=True), node.get("tls_fingerprint"))
        path = f"/api2/json/nodes/{node['pve_node']}/qemu/{int(vmid)}/vncproxy"
        conn.request("POST", path, body=urlencode({"websocket": 1}),
                     headers={"Authorization": f"PVEAPIToken={node['token']}",
                              "Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        body = resp.read()
    except (OSError, http.client.HTTPException) as e:
        raise ConsoleError(f"Proxmox unreachable ({type(e).__name__}: {e})") from e
    finally:
        conn.close()
    if resp.status != 200:
        raise ConsoleError(f"Proxmox refused the console (HTTP {resp.status}: "
                           f"{body[:120].decode(errors='replace')})")
    data = json.loads(body).get("data") or {}
    if not data.get("port") or not data.get("ticket"):
        raise ConsoleError("Proxmox returned no console port/ticket")
    return {"port": int(data["port"]), "ticket": str(data["ticket"])}


def upstream_url(node, vmid, port, ticket):
    host, api_port = node_host(node)
    return (f"wss://{host}:{api_port}/api2/json/nodes/{node['pve_node']}/qemu/{int(vmid)}"
            f"/vncwebsocket?port={int(port)}&vncticket={quote(ticket, safe='')}")


async def _default_connect(url, headers, node):
    from websockets.asyncio.client import connect

    # ping_interval=None: pveproxy's websocket is a plain relay and is not relied on to
    # answer pings; a missed pong would tear down a healthy console mid-session.
    ws = await connect(url, additional_headers=headers, ssl=_ssl_context(),
                       subprotocols=["binary"], max_size=None, ping_interval=None,
                       open_timeout=15)
    try:
        sslobj = ws.transport.get_extra_info("ssl_object")
        _check_fingerprint(sslobj.getpeercert(binary_form=True) if sslobj else b"",
                           node.get("tls_fingerprint"))
    except Exception:
        await ws.close()
        raise
    return ws


class ConsoleBroker:
    """Mints console tickets and holds the one-shot relay table.

    `nodes` maps a node key (the box records' `node`) to
    {endpoint, pve_node, token, tls_fingerprint?}. `proxy` and `connect` are injectable so the
    whole flow runs offline in tests."""

    def __init__(self, nodes, ttl=RELAY_TTL, proxy=vncproxy, connect=_default_connect,
                 clock=time.monotonic):
        self.nodes = dict(nodes or {})
        self.ttl = ttl
        self._proxy = proxy
        self._connect = connect
        self._clock = clock
        self._pending = {}
        self._lock = threading.Lock()

    def available(self, box=None):
        """True when a console can be minted (for `box`, or for any box)."""
        if box is None:
            return bool(self.nodes)
        node = self.nodes.get(box.get("node"))
        return bool(node and node.get("token") and box.get("vmid"))

    def _sweep(self, now):
        for rid in [r for r, e in self._pending.items() if e["expires"] <= now]:
            del self._pending[rid]

    async def mint(self, box, owner):
        """Ticket for `box`; returns {relay_id, password}. `owner` is bound to the relay id so
        only the session that minted it can open it."""
        if not self.available(box):
            raise ConsoleError("console access is not configured for this box")
        node = self.nodes[box["node"]]
        got = await asyncio.to_thread(self._proxy, node, box["vmid"])
        relay_id = secrets.token_urlsafe(24)
        with self._lock:
            now = self._clock()
            self._sweep(now)
            self._pending[relay_id] = {"node": box["node"], "vmid": int(box["vmid"]),
                                       "port": got["port"], "ticket": got["ticket"],
                                       "owner": owner, "box": box.get("name"),
                                       "expires": now + self.ttl}
        return {"relay_id": relay_id, "password": got["ticket"]}

    def take(self, relay_id, owner):
        """Pop a relay entry; None when unknown, expired, already used, or not `owner`'s.

        A wrong-owner attempt still consumes the id: a leaked relay id is burned by the
        first try, whoever makes it."""
        with self._lock:
            now = self._clock()
            self._sweep(now)
            entry = self._pending.pop(relay_id, None)
        if entry is None or entry["owner"] != owner:
            return None
        return entry

    async def relay(self, client, entry):
        """Pipe frames between the browser websocket (Starlette) and Proxmox until either
        side closes. `client` must already be accepted."""
        node = self.nodes[entry["node"]]
        url = upstream_url(node, entry["vmid"], entry["port"], entry["ticket"])
        upstream = await self._connect(url, {"Authorization": f"PVEAPIToken={node['token']}"},
                                       node)

        async def browser_to_pve():
            while True:
                msg = await client.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
                elif msg.get("text") is not None:
                    await upstream.send(msg["text"])

        async def pve_to_browser():
            async for data in upstream:
                if isinstance(data, (bytes, bytearray)):
                    await client.send_bytes(bytes(data))
                else:
                    await client.send_text(data)

        tasks = [asyncio.create_task(browser_to_pve()), asyncio.create_task(pve_to_browser())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await upstream.close()
            except Exception:  # noqa: BLE001 - closing a dead socket is not news
                pass
