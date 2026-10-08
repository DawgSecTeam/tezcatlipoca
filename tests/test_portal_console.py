"""Console broker (portal/console.py): ticket mint, one-shot relay ids, frame relay — offline."""

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portal.console import ConsoleBroker, ConsoleError, upstream_url  # noqa: E402

NODE = {"endpoint": "https://10.0.0.150:8006", "pve_node": "proxmox",
        "token": "tezcon-c-run-1@pve!portal=s3cret", "tls_fingerprint": ""}
BOX = {"name": "web01", "vmid": 1210, "node": "pve150"}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _broker(**kw):
    calls = []

    def proxy(node, vmid):
        calls.append((node["pve_node"], vmid))
        return {"port": 5901, "ticket": "PVEVNC:abc+/="}

    b = ConsoleBroker({"pve150": NODE}, proxy=proxy, **kw)
    return b, calls


class MintAndTake(unittest.TestCase):
    def test_mint_calls_vncproxy_on_the_boxes_node(self):
        b, calls = _broker()
        minted = asyncio.run(b.mint(BOX, "team1:1"))
        self.assertEqual(calls, [("proxmox", 1210)])
        self.assertEqual(minted["password"], "PVEVNC:abc+/=")
        entry = b.take(minted["relay_id"], "team1:1")
        self.assertEqual((entry["vmid"], entry["port"]), (1210, 5901))

    def test_relay_ids_are_single_use(self):
        b, _ = _broker()
        rid = asyncio.run(b.mint(BOX, "team1:1"))["relay_id"]
        self.assertIsNotNone(b.take(rid, "team1:1"))
        self.assertIsNone(b.take(rid, "team1:1"))

    def test_wrong_owner_is_refused_and_burns_the_id(self):
        b, _ = _broker()
        rid = asyncio.run(b.mint(BOX, "team1:1"))["relay_id"]
        self.assertIsNone(b.take(rid, "team2:9"))
        self.assertIsNone(b.take(rid, "team1:1"))

    def test_relay_ids_expire(self):
        clock = Clock()
        b, _ = _broker(clock=clock, ttl=30)
        rid = asyncio.run(b.mint(BOX, "o"))["relay_id"]
        clock.t += 31
        self.assertIsNone(b.take(rid, "o"))

    def test_box_without_a_tokened_node_cannot_mint(self):
        b, calls = _broker()
        other = {**BOX, "node": "pve193"}
        self.assertFalse(b.available(other))
        with self.assertRaises(ConsoleError):
            asyncio.run(b.mint(other, "o"))
        self.assertEqual(calls, [])
        self.assertFalse(ConsoleBroker({}).available())

    def test_upstream_url_matches_the_proven_pfsense_tool_shape(self):
        url = upstream_url(NODE, 1210, 5901, "PVEVNC:abc+/=")
        self.assertEqual(url, "wss://10.0.0.150:8006/api2/json/nodes/proxmox/qemu/1210/"
                              "vncwebsocket?port=5901&vncticket=PVEVNC%3Aabc%2B%2F%3D")


class FakeClient:
    """Starlette-WebSocket-shaped browser side."""

    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    async def receive(self):
        await asyncio.sleep(0)
        if self.incoming:
            return {"type": "websocket.receive", "bytes": self.incoming.pop(0)}
        await asyncio.sleep(0.05)
        return {"type": "websocket.disconnect"}

    async def send_bytes(self, data):
        self.sent.append(data)

    async def send_text(self, data):
        self.sent.append(data)


class FakeUpstream:
    def __init__(self, frames):
        self.frames = list(frames)
        self.got = []
        self.closed = False

    async def send(self, data):
        self.got.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(1)
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


class Relay(unittest.TestCase):
    def test_frames_flow_both_ways_with_the_token_header(self):
        up = FakeUpstream([b"RFB 003.008\n"])
        seen = {}

        async def connect(url, headers, node):
            seen["url"], seen["headers"] = url, headers
            return up

        b = ConsoleBroker({"pve150": NODE}, proxy=lambda n, v: {"port": 5901, "ticket": "t"},
                          connect=connect)
        client = FakeClient([b"RFB 003.003\n"])

        async def go():
            rid = (await b.mint(BOX, "o"))["relay_id"]
            await b.relay(client, b.take(rid, "o"))

        asyncio.run(go())
        self.assertEqual(seen["headers"], {"Authorization": "PVEAPIToken=" + NODE["token"]})
        self.assertIn("vncticket=t", seen["url"])
        self.assertEqual(client.sent, [b"RFB 003.008\n"])
        self.assertEqual(up.got, [b"RFB 003.003\n"])
        self.assertTrue(up.closed)


if __name__ == "__main__":
    unittest.main()
