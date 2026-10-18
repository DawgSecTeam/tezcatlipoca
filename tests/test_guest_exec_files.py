"""Guest-agent file transfer instead of scp/ssh.

The agent file endpoints are the only file path that needs no network into the guest,
but neither has an append/offset, and file-write's `content` is capped at 61440 chars
(apidoc maxLength), so anything larger travels as .partN files reassembled with a
guest-side `cat`. file-read is asymmetric: the apidoc default `decode=1` means the
`content` we get back is already decoded, and it arrives as a str — non-UTF-8 bytes
only survive if we re-read it with surrogateescape (the same contract pmx.py's
`read`/`write` bake in).

Offline; the Proxmox API is faked and no VM is touched.
"""

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from infra import guest_exec  # noqa: E402


class _FakeApi:
# Scripted proxmox_api: records every call, answers writes and exec, serves read data.
# `content` is serialized the way the API does it — the decoded bytes ride a UTF-8 JSON
# string — so the read tests exercise the real str -> bytes contract, not a byte array.

    def __init__(self, content=b""):
        self.calls = []
        self.content = content

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/agent/file-write"):
            return None                     # apidoc: returns null
        if path.endswith("/agent/file-read"):
            served = json.loads(json.dumps(self.content.decode("utf-8", "surrogateescape")))
            return {"data": {"content": served}}
        if path.endswith("/agent/exec"):
            return {"data": {"pid": 1}}
        if path.endswith("/agent/exec-status"):
            return {"data": {"exited": 1, "exitcode": 0}}
        raise AssertionError(f"unexpected call {method} {path}")

    def writes(self):
        return [kw["data"] for m, p, kw in self.calls if p.endswith("/agent/file-write")]


class ExecChannelLoss(unittest.TestCase):
    def test_a_lost_pid_is_a_lost_channel_not_an_httperror(self):
        from requests import HTTPError
        with patch.object(guest_exec, "proxmox_api") as api:
            api.side_effect = [
                {"data": {"pid": 9}},
                HTTPError("500 Server Error: Agent error: PID 9 does not exist"),
            ]
            with self.assertRaises(RuntimeError) as cm:
                guest_exec.guest_agent_exec_root("pve", 1, "true", timeout=5)
        self.assertIn("lost exec pid 9", str(cm.exception))


class WriteToGuest(unittest.TestCase):
    def test_chunks_stay_under_the_documented_content_cap(self):
        size = guest_exec._FILE_WRITE_CHUNK * 2 + 10          # guarantees 3 chunks
        api = _FakeApi()
        with patch.object(guest_exec, "proxmox_api", api):
            guest_exec.guest_agent_write_file("pve", 221, "/tmp/blob", bytes(size))
        sent = api.writes()
        self.assertEqual(len(sent), 3)
        self.assertEqual([len(w["content"]) for w in sent][:2],
                         [61440, 61440], "base64 of a full chunk must hit the cap exactly")
        for w in sent:
            self.assertLessEqual(len(w["content"]), 61440,
                                 "apidoc file-write content maxLength is 61440")

    def test_multi_chunk_write_reassembles_byte_identically_from_bytes(self):
        data = bytes(range(256)) * (guest_exec._FILE_WRITE_CHUNK * 2 // 256) + b"tail"
        self.assertGreater(len(data), guest_exec._FILE_WRITE_CHUNK, "must need >1 chunk")
        api = _FakeApi()
        with patch.object(guest_exec, "proxmox_api", api):
            guest_exec.guest_agent_write_file("pve", 221, "/root/payload.bin", data)
        sent = api.writes()
        self.assertGreater(len(sent), 1)
        self.assertEqual([w["file"] for w in sent],
                         [f"/root/payload.bin.part{i}" for i in range(len(sent))])
        reassembled = b"".join(base64.b64decode(w["content"]) for w in sent)
        self.assertEqual(reassembled, data)
        for w in sent:
            self.assertEqual(w["encode"], 0, "encode=0 = content is already base64")

    def test_local_file_path_is_read_and_sent(self):
        payload = b"from a file\n\x00\xff"
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "payload"
            local.write_bytes(payload)
            api = _FakeApi()
            with patch.object(guest_exec, "proxmox_api", api):
                guest_exec.guest_agent_write_file("pve", 221, "/root/payload", local)
        (sent,) = api.writes()
        self.assertEqual(sent["file"], "/root/payload.part0")
        self.assertEqual(base64.b64decode(sent["content"]), payload)

    def test_endpoint_and_params_and_guest_side_reassembly(self):
        api = _FakeApi()
        with patch.object(guest_exec, "proxmox_api", api):
            guest_exec.guest_agent_write_file("pve", 221, "/root/x", b"hi")
        method, path, kwargs = api.calls[0]
        self.assertEqual((method, path), ("POST", "/nodes/pve/qemu/221/agent/file-write"))
        self.assertEqual(set(kwargs["data"]), {"file", "content", "encode"})
        self.assertEqual(kwargs["data"]["file"], "/root/x.part0")
        self.assertEqual(base64.b64decode(kwargs["data"]["content"]), b"hi")
        (exec_post,) = [c for c in api.calls if c[1].endswith("/agent/exec")]
        self.assertEqual(exec_post[0], "POST")
        self.assertEqual(
            exec_post[2]["data"]["command"],
            ["bash", "-c", "cat /root/x.part0 > /root/x && rm -f /root/x.part0"],
        )

    def test_empty_file_still_creates_the_target(self):
        api = _FakeApi()
        with patch.object(guest_exec, "proxmox_api", api):
            guest_exec.guest_agent_write_file("pve", 221, "/root/empty", b"")
        self.assertEqual(api.writes(), [], "an empty file needs no chunk")
        (exec_post,) = [c for c in api.calls if c[1].endswith("/agent/exec")]
        self.assertEqual(
            exec_post[2]["data"]["command"],
            ["bash", "-c", "cat  > /root/empty && rm -f "],
        )

    def test_failed_reassembly_raises(self):
        class _FailApi(_FakeApi):
            def __call__(self, method, path, **kwargs):
                if path.endswith("/agent/exec-status"):
                    self.calls.append((method, path, kwargs))
                    return {"data": {"exited": 1, "exitcode": 1, "err-data": "cat: nope"}}
                return super().__call__(method, path, **kwargs)

        with patch.object(guest_exec, "proxmox_api", _FailApi()):
            with self.assertRaises(RuntimeError) as caught:
                guest_exec.guest_agent_write_file("pve", 221, "/root/x", b"hi")
        self.assertIn("cat: nope", str(caught.exception))


class ReadFromGuest(unittest.TestCase):
    def test_binary_content_round_trips(self):
        # NUL, C1/control bytes and multi-byte UTF-8: non-text but still valid on the
        # API's UTF-8 JSON string. Bytes that are not valid UTF-8 cannot survive the
        # JSON response at all (see the report's unverified note).
        payload = (bytes(range(0, 32)) + bytes(range(0x7F, 0x100))
                   + "é€\U0001f600".encode("utf-8"))
        api = _FakeApi(payload)
        with patch.object(guest_exec, "proxmox_api", api):
            data = guest_exec.guest_agent_read_file("pve", 221, "/root/blob.bin")
        self.assertEqual(data, payload)

    def test_endpoint_and_params(self):
        api = _FakeApi(b"hi")
        with patch.object(guest_exec, "proxmox_api", api):
            guest_exec.guest_agent_read_file("pve", 221, "/root/blob.bin")
        (method, path, kwargs) = api.calls[0]
        self.assertEqual((method, path), ("GET", "/nodes/pve/qemu/221/agent/file-read"))
        # decode (default 1) already decoded the agent's base64; count/offset stay default.
        self.assertEqual(kwargs["params"], {"file": "/root/blob.bin"})

    def test_local_path_writes_the_bytes(self):
        payload = b"\x00\x01\x7f\xc3\xa9"          # what the wire actually carries
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "pulled.bin"
            with patch.object(guest_exec, "proxmox_api", _FakeApi(payload)):
                result = guest_exec.guest_agent_read_file("pve", 221, "/root/blob.bin", local)
            self.assertIsNone(result)
            self.assertEqual(local.read_bytes(), payload)

    def test_empty_guest_file_reads_as_empty_bytes(self):
        with patch.object(guest_exec, "proxmox_api", _FakeApi(b"")):
            self.assertEqual(guest_exec.guest_agent_read_file("pve", 221, "/root/empty"), b"")


if __name__ == "__main__":
    unittest.main()
