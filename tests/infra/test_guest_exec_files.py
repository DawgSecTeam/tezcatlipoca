"""Guest-agent file transfer instead of scp/ssh; offline, the Proxmox API is faked."""

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from infra import guest_exec  # noqa: E402


class _FakeApi:
# Fakes proxmox_api. Content rides a UTF-8 JSON string like the real API's str->bytes contract.

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
        size = guest_exec._FILE_WRITE_CHUNK * 2 + 10
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
        # NUL, C1 and multi-byte UTF-8 are valid on the API's JSON string; non-UTF-8 bytes are not.
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




if __name__ == "__main__":
    unittest.main()
