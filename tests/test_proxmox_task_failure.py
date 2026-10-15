"""A failed PVE task must end the wait, and a transient datastore-lock clone must be retried.

Live-found 2026-10-15 (three concurrent campaign deploys): PVE reported the loser of a
storage-lock race as `{"status": "clone failed: can't lock file
'/var/lock/pve-manager/pve-storage-ssd' - got timeout"}` with no `exitstatus`, so the
`status == "stopped"` test never fired. One run died in 7 s; the other two polled the
corpse for the full 1800 s timeout while holding their workspaces. The clone itself is
idempotent and the lock transient, so it is retried rather than failed.

Requests are routed through `infra.client`'s own per-node override seam, so the real
`proxmox_api`/`destroy_vm_if_exists`/`_is_template` path is exercised with no config and
no network.
"""

import sys
import unittest

import requests
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from infra import client  # noqa: E402
from engine import engine_template_ops  # noqa: E402

UPID = "UPID:n:1:2:3:qmclone:955:x:"
LOCK_MESSAGE = ("Proxmox task UPID:x failed: clone failed: can't lock file "
                "'/var/lock/pve-manager/pve-storage-ssd' - got timeout")


class WaitForTaskTerminalStates(unittest.TestCase):
    def test_worker_failure_in_status_raises_and_does_not_poll(self):
        calls = []

        def api(method, path, **kw):
            calls.append(path)
            return {"data": {"status": "clone failed: can't lock file "
                                       "'/var/lock/pve-manager/pve-storage-ssd' - got timeout"}}

        with patch.object(client, "proxmox_api", api), \
                patch.object(client.time, "sleep", lambda *_: None):
            with self.assertRaises(RuntimeError) as ctx:
                client.wait_for_proxmox_task("n", UPID)
        self.assertIn("lock file", str(ctx.exception))
        self.assertEqual(len(calls), 1, "a terminal status must not be polled again")

    def test_completed_with_warnings_still_passes(self):
        with patch.object(client, "proxmox_api", lambda m, p, **k: {"data": {"status": "stopped"}}), \
                patch.object(client.time, "sleep", lambda *_: None):
            self.assertIsNone(client.wait_for_proxmox_task("n", UPID))


class FakeCluster:
    """Routes every proxmox_api call through client's override seam.

    `on_clone` is called per clone POST and decides whether that attempt's wait fails.
    """

    def __init__(self, vms_after_failure=(), fail_first=False):
        self.calls, self.partial_shown = [], False
        self.vms_after_failure, self.fail_first = list(vms_after_failure), fail_first
        self.clones = 0
        self.destroys = []

    def fake_api_for(self, endpoint, token, method, path, **kw):
        self.calls.append((method, path))
        if method == "POST" and path.endswith("/clone"):
            self.clones += 1
            return {"data": UPID}
        if method == "GET" and path == "/nodes/n/qemu":
            # Before the first clone the slot is clean; the partial clone only appears
            # once a clone attempt has been made, like the real storage-lock race.
            if self.fail_first and not self.clones:
                return {"data": []}
            return {"data": list(self.vms_after_failure)}
        if method == "GET" and path.endswith("/config"):
            return {"data": {"tags": "", "lock": "clone"}}
        if path.endswith("/tasks/") and not path.endswith("/status"):
            return {"data": UPID}
        if path.endswith("/tasks/" + UPID + "/status") or path.endswith("/tasks/None/status"):
            # reclaims of the partial clone go through the real destroyer, whose own
            # task waits must succeed (its stop/delete POSTs return None here)
            return {"data": {"status": "stopped", "exitstatus": "OK"}}
        if method == "DELETE":
            self.destroys.append(path)
            return {"data": None}
        return {"data": {}}


class EngineTemplateCloneRetry(unittest.TestCase):
    def _run(self, cluster, wait):
        stack = ExitStack()
        stack.enter_context(patch.object(client, "_route_for_path",
                                        lambda path: ("https://h:8006", "tok")))
        stack.enter_context(patch.object(client, "proxmox_api_for", cluster.fake_api_for))
        stack.enter_context(patch.object(engine_template_ops, "wait_for_proxmox_task", wait))
        stack.enter_context(patch.object(engine_template_ops.time, "sleep", lambda *_: None))
        with stack:
            engine_template_ops.build_engine_template(
                "n", "/tmp/comp", 4000, 955, {}, "pg", "redis", "ref", "hash", {})

    def test_transient_lock_failure_reclaims_partial_and_retries(self):
        cluster = FakeCluster(vms_after_failure=[{"vmid": 4140, "name": "engine-template"}],
                              fail_first=True)
        attempts = []

        def wait(node, upid):
            attempts.append(upid)
            if len(attempts) == 1:
                raise RuntimeError(LOCK_MESSAGE)

        # The second attempt gets through the clone and the config PUT; the build then
        # fails later (no real host to SSH), which is what proves it got past the retry.
        with self.assertRaises(Exception):
            self._run(cluster, wait)
        self.assertEqual(cluster.clones, 2, "lock contention is retried, not failed")
        self.assertIn("DELETE", [m for m, _ in cluster.calls], "the locked partial is reclaimed")

    def test_lock_failure_is_bounded_but_persists_past_the_contention(self):
        # The retry must OUTLAST the contention window, not merely repeat: an earlier flat
        # 20 s gap exhausted its three attempts inside the window (run-2631ec27, 2026-10-15)
        # while the winners' clones committed seconds after it started. Assert the wait, not
        # just the count — a future edit that shortens the backoff reintroduces that failure.
        cluster = FakeCluster(vms_after_failure=[{"vmid": 4140, "name": "engine-template"}],
                              fail_first=True)
        slept = []

        def wait(node, upid):
            raise RuntimeError(LOCK_MESSAGE)

        stack = ExitStack()
        stack.enter_context(patch.object(client, "_route_for_path",
                                         lambda path: ("https://h:8006", "tok")))
        stack.enter_context(patch.object(client, "proxmox_api_for", cluster.fake_api_for))
        stack.enter_context(patch.object(engine_template_ops, "wait_for_proxmox_task", wait))
        stack.enter_context(patch.object(engine_template_ops.time, "sleep", slept.append))
        with stack:
            with self.assertRaises(RuntimeError) as ctx:
                engine_template_ops.build_engine_template(
                    "n", "/tmp/comp", 4000, 955, {}, "pg", "redis", "ref", "hash", {})
        self.assertIn("lock file", str(ctx.exception))
        self.assertEqual(cluster.clones, 5, "the retry is bounded at five attempts")
        self.assertGreaterEqual(sum(slept), 300,
                                "total backoff must give the datastore queue minutes to drain")

    def test_non_lock_failure_is_not_retried(self):
        cluster = FakeCluster()

        def wait(node, upid):
            raise RuntimeError("Proxmox task UPID:x failed: storage 'hdd' does not exist")

        with self.assertRaises(RuntimeError):
            self._run(cluster, wait)
        self.assertEqual(cluster.clones, 1, "a real failure is surfaced, not retried")


if __name__ == "__main__":
    unittest.main()


class LockContentionRetry(unittest.TestCase):
    """PVE answers the loser of a lock race with 500 'can't lock file ... - got timeout'.

    Live-found 2026-10-15, run-6cd31cc3: three parallel deploys, the phase-4 golden config
    PUT died on `/var/lock/qemu-server/lock-2350.conf`. The request is idempotent and the
    lock is held only while the winner's operation runs, so `proxmox_api_for` retries it —
    but only it: a real 500 must still surface.
    """

    def _response(self, text, status=500):
        class R:
            pass
        r = R()
        r.text, r.status_code = text, status
        r.json = lambda: {"data": {"ok": True}}

        def raise_for_status():
            if status >= 400:
                raise requests.exceptions.HTTPError(f"{status} Server Error: {text}", response=r)
        r.raise_for_status = raise_for_status
        return r

    def test_lock_500_is_retried_then_succeeds(self):
        seen = []

        def request(method, url, **kw):
            seen.append(url)
            if len(seen) == 1:
                return self._response("can't lock file '/var/lock/qemu-server/lock-2350.conf'"
                                      " - got timeout")
            return self._response("", status=200)

        with patch.object(client, "proxmox_request", request), \
                patch.object(client.time, "sleep", lambda *_: None):
            out = client.proxmox_api_for("https://h:8006", "tok", "PUT",
                                         "/nodes/p/qemu/2350/config", data={})
        self.assertEqual(out, {"data": {"ok": True}})
        self.assertEqual(len(seen), 2)

    def test_other_500_is_not_retried(self):
        calls = []

        def request(method, url, **kw):
            calls.append(url)
            return self._response("storage 'hdd' does not exist")

        with patch.object(client, "proxmox_request", request), \
                patch.object(client.time, "sleep", lambda *_: None):
            with self.assertRaises(requests.exceptions.HTTPError):
                client.proxmox_api_for("https://h:8006", "tok", "PUT",
                                       "/nodes/p/qemu/2350/config", data={})
        self.assertEqual(len(calls), 1)

    def test_retry_predicate_leaves_other_callers_alone(self):
        from common.utils import retry
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("not retryable")
        with self.assertRaises(RuntimeError):
            retry(boom, attempts=3, gap=0, retry_on=ValueError)
        self.assertEqual(len(calls), 1)

        calls.clear()

        def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise ValueError("retryable")
            return "ok"
        self.assertEqual(retry(flaky, attempts=3, gap=0,
                               retry_on=lambda e: isinstance(e, ValueError)), "ok")
        self.assertEqual(len(calls), 2)
