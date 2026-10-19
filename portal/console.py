"""Proxmox web console: mint a VNC ticket per click and relay the websocket."""

import asyncio
import hashlib
import json
import secrets
import ssl
import threading
import time
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

RELAY_TTL = 30  # seconds between mint and the browser's websocket open


class _FingerprintAdapter(HTTPAdapter):
    # TLS pinning by peer-cert sha256 — the same shape as infra.client._FingerprintAdapter.
    def __init__(self, fingerprint, **kw):
        self._fingerprint = fingerprint
        super().__init__(**kw)

    def init_poolmanager(self, connections, maxsize, block=False, **kw):
        self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block,
                                       assert_fingerprint=self._fingerprint, **kw)


def vncproxy_mint(endpoint, token, pve_node, vmid, fingerprint=None, timeout=30):
    """Mint a PVE noVNC ticket: POST /nodes/<pve_node>/qemu/<vmid>/vncproxy {websocket: 1}.

    Returns {port: int, ticket: str}. Self-contained by necessity: the portal image ships only
    portal/, and importing `infra` drags the deploy driver into the container.
    """
    url = f"{endpoint.rstrip('/')}/api2/json/nodes/{pve_node}/qemu/{int(vmid)}/vncproxy"
    session = requests.Session()
    if fingerprint:
        session.mount("https://", _FingerprintAdapter(fingerprint.replace(":", "").strip().lower()))
    r = session.post(url, data={"websocket": 1},
                     headers={"Authorization": f"PVEAPIToken={token}"},
                     verify=False, timeout=timeout)
    r.raise_for_status()
    data = r.json().get("data") or {}
    if not data.get("port") or not data.get("ticket"):
        raise ValueError("Proxmox returned no console port/ticket")
    return {"port": int(data["port"]), "ticket": str(data["ticket"])}


class ConsoleError(RuntimeError):
    pass


def _ssl_context():
    # Verified by fingerprint (below) or not at all — these hosts have no CA.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _check_fingerprint(der_cert, want):
    want = (want or "").replace(":", "").strip().lower()
    if not want:
        return
    got = hashlib.sha256(der_cert or b"").hexdigest()
    if got != want:
        raise ConsoleError(f"Proxmox TLS fingerprint mismatch (got {got[:16]}…)")


def node_host(node):
    u = urlparse(node["endpoint"])
    return u.hostname, u.port or 8006


# blocking — call via to_thread
def vncproxy(node, vmid, timeout=20):
    try:
        return vncproxy_mint(node["endpoint"], node["token"], node["pve_node"], vmid,
                             fingerprint=node.get("tls_fingerprint"), timeout=timeout)
    except requests.HTTPError as e:
        resp = e.response
        raise ConsoleError(f"Proxmox refused the console (HTTP {resp.status_code}: "
                           f"{(resp.text or '')[:120]})") from e
    except ValueError as e:
        raise ConsoleError(str(e)) from e
    except OSError as e:
        raise ConsoleError(f"Proxmox unreachable ({type(e).__name__}: {e})") from e


def upstream_url(node, vmid, port, ticket):
    host, api_port = node_host(node)
    return (f"wss://{host}:{api_port}/api2/json/nodes/{node['pve_node']}/qemu/{int(vmid)}"
            f"/vncwebsocket?port={int(port)}&vncticket={quote(ticket, safe='')}")


async def _default_connect(url, headers, node):
    from websockets.asyncio.client import connect

    # ping_interval=None: pveproxy never answers pings, and a missed pong tears down a healthy
    # console mid-session.
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


# `proxy`/`connect` are injectable so the whole flow runs offline in tests
class ConsoleBroker:
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
        if box is None:
            return bool(self.nodes)
        node = self.nodes.get(box.get("node"))
        return bool(node and node.get("token") and box.get("vmid"))

    def _sweep(self, now):
        for rid in [r for r, e in self._pending.items() if e["expires"] <= now]:
            del self._pending[rid]

    # `owner` is bound to the relay id, so only the minting session can open it
    async def mint(self, box, owner):
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

    # a wrong-owner attempt still consumes the id: a leaked relay id is burned by the first try
    def take(self, relay_id, owner):
        with self._lock:
            now = self._clock()
            self._sweep(now)
            entry = self._pending.pop(relay_id, None)
        if entry is None or entry["owner"] != owner:
            return None
        return entry

    # `client` must already be accepted
    async def relay(self, client, entry):
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
