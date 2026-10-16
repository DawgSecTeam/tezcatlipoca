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

import contextlib
import io
import os
import sys
import tempfile
import threading
import time
import unittest

import requests
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from infra import client  # noqa: E402
from infra import lifecycle  # noqa: E402
from engine import engine_template_ops  # noqa: E402
from golden import golden_ops  # noqa: E402

COMP_NAME = "tlaloc-smoke"

UPID = "UPID:n:1:2:3:qmclone:955:x:"
LOCK_MESSAGE = ("Proxmox task UPID:x failed: clone failed: can't lock file "
                "'/var/lock/pve-manager/pve-storage-ssd' - got timeout")
VM_LOCK_MESSAGE = ("Proxmox task UPID:x failed: can't lock file "
                   "'/var/lock/qemu-server/lock-2351.conf' - got timeout")


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


class GoldenCloneQueueRetry(unittest.TestCase):
    """A GOLDEN clone takes PVE's datastore lock too, so it must queue like the
    conversion beside it.

    Live 2026-10-16: phases 1-3 of a clean tlaloc-smoke deploy were fine, then phase 4
    died with `can't lock file '/var/lock/pve-manager/pve-storage-hdd' - got timeout`
    while four concurrent clones ran on a node at load 22 and two other comps deployed.
    `storage_queue_lock`'s own charter names "base-image clone" and the conversion path
    already used it — this call site never did.
    """

    def _state_api(self):
        state = {"calls": [], "clones": 0, "destroys": [], "partial": False}

        def fake(endpoint, token, method, path, **kw):
            state["calls"].append((method, path))
            if method == "POST" and path.endswith("/clone"):
                state["clones"] += 1
                state["partial"] = True
                return {"data": UPID}
            if method == "GET" and path == "/nodes/n/qemu":
                # the half-copy only appears once a clone has been attempted
                return {"data": [{"vmid": 1150, "name": "golden-web01"}] if state["partial"] else []}
            if method == "GET" and path.endswith("/config"):
                return {"data": {"lock": "clone",
                                 "description": f"tezcatlipoca-clone comp-{COMP_NAME}"}}
            if method == "DELETE":
                state["destroys"].append(path)
                state["partial"] = False
                return {"data": None}
            if path.endswith("/status"):
                return {"data": {"status": "stopped", "exitstatus": "OK"}}
            if method == "GET":
                return {"data": []}
            return {"data": {}}
        return state, fake

    def _run(self, state, fake, wait, queued):
        comp = Path(tempfile.mkdtemp())
        (comp / "Compfile").write_text(f"name {COMP_NAME}\nteams 1\n")
        templates = {"web-tpl": 955}
        missing = [{"vmid": 1150, "vm_name": "golden-web01", "ip": "192.168.190.50",
                    "gateway": "192.168.190.1", "bridge": "vmbr190",
                    "box": {"template": "web-tpl", "name": "web01"}}]
        stack = ExitStack()
        stack.enter_context(patch.object(client, "_route_for_path",
                                         lambda path: ("https://h:8006", "tok")))
        stack.enter_context(patch.object(client, "proxmox_api_for", fake))
        stack.enter_context(patch.object(golden_ops, "wait_for_proxmox_task", wait))
        stack.enter_context(patch.object(golden_ops.time, "sleep", lambda *_: None))
        stack.enter_context(patch.object(golden_ops, "storage_queue_lock", queued))
        with stack:
            golden_ops._clone_missing_goldens("n", missing, templates, str(comp),
                                              {"box_username": "ubuntu"}, "pw", "tags")

    def test_the_clone_queues_and_a_lock_failure_is_retried(self):
        state, fake = self._state_api()
        entered = []

        def queued(node, wait_s=900):
            entered.append(node)
            return contextlib.nullcontext()

        def wait(node, upid, **kw):
            if state["clones"] == 1:
                raise RuntimeError(LOCK_MESSAGE)

        self._run(state, fake, wait, queued)
        self.assertTrue(entered, "the golden clone must take the datastore queue lock")
        self.assertEqual(state["clones"], 2, "lock contention is retried, not failed")
        self.assertTrue(state["destroys"], "the locked half-clone is reclaimed before the retry")

    def test_a_non_lock_failure_is_not_retried(self):
        state, fake = self._state_api()

        def wait(node, upid, **kw):
            raise RuntimeError("Proxmox task UPID:x failed: storage 'hdd' does not exist")

        with self.assertRaises(RuntimeError):
            self._run(state, fake, wait, lambda node, wait_s=900: contextlib.nullcontext())
        self.assertEqual(state["clones"], 1, "a real failure is surfaced, not retried")


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


class StorageQueueLock(unittest.TestCase):
    """First-time storage-heavy PVE ops on one node queue instead of racing for the datastore.

    Live 2026-10-16 (the second three-range campaign): three base-image clones contended, the
    loser needed four of its five retries, and a golden's template CONVERSION lost the same lock
    and failed — leaving `template: 1` with the volume never renamed, which the deploy only
    noticed at the volume guard. Retrying harder is a worse answer than not racing.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(Path, "home", return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _lock(self, node="n", **kw):
        return client.storage_queue_lock(node, **kw)

    def test_a_second_op_on_the_same_node_waits_for_the_first(self):
        order, held = [], threading.Event()

        def first():
            with self._lock():
                order.append("first-in")
                held.set()
                time.sleep(0.3)
                order.append("first-out")

        def second():
            held.wait(5)
            with self._lock():
                order.append("second-in")

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(order, ["first-in", "first-out", "second-in"],
                         "the second holder must not enter while the first is inside")

    def test_a_different_node_is_not_serialised_behind_it(self):
        order = []
        with self._lock("n1"):
            with self._lock("n2"):
                order.append("both-held")
        self.assertEqual(order, ["both-held"], "the lock is per node, not global")

    def test_a_wedged_holder_does_not_block_a_deploy_forever(self):
        # Something alive but stuck must not wedge every later deploy: a wait past the bound
        # proceeds and leaves the contention to the operation's own retry.
        with self._lock():
            with self._lock(wait_s=0):
                entered = True
        self.assertTrue(entered)

    def test_the_holder_is_named_so_a_wait_is_diagnosable(self):
        with self._lock():
            holder = [p for p in (Path(self.tmp.name) / ".tezcatlipoca" / "locks").iterdir()][0]
            self.assertEqual(holder.read_text().strip(), str(os.getpid()))


class TemplateConversionRetry(unittest.TestCase):
    """A conversion that loses PVE's datastore lock must be retried, not left half-converted.

    The failed task still leaves `template: 1` and the volume un-renamed, so the only other
    notice is the volume guard — which fails the whole deploy (live 2026-10-16, the DC golden
    under three concurrent ranges).
    """

    def _convert(self, lock_failures):
        from golden import golden_ops
        posts, waits = [], []

        def wait(node, upid):
            waits.append(upid)
            if len(waits) <= lock_failures:
                raise RuntimeError("Proxmox task UPID:x failed: can't lock file "
                                   "'/var/lock/pve-manager/pve-storage-hdd' - got timeout")

        target = {"vmid": 2350, "vm_name": "golden-dc01", "box": {"name": "dc01"}}
        with patch.object(golden_ops, "list_snapshots", lambda *a: []), \
             patch.object(golden_ops, "proxmox_api",
                          side_effect=lambda *a, **k: posts.append(a) or {"data": "UPID:x"}), \
             patch.object(golden_ops, "wait_for_proxmox_task", wait), \
             patch.object(golden_ops, "storage_queue_lock", contextlib.nullcontext), \
             patch.object(golden_ops, "_await_base_volumes", lambda *a: None), \
             patch.object(golden_ops.time, "sleep", lambda *_: None), \
             contextlib.redirect_stdout(io.StringIO()):
            golden_ops._convert_goldens("n", [target], "tag", {})
        return [a for a in posts if a[0] == "POST"], waits

    def test_a_lock_failure_is_retried(self):
        posts, waits = self._convert(lock_failures=1)
        self.assertEqual(len(posts), 2, "the conversion is attempted again after a lost lock")
        self.assertEqual(len(waits), 2)

    def test_a_conversion_that_keeps_losing_the_lock_gives_up(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._convert(lock_failures=5)
        self.assertIn("lock file", str(ctx.exception))


class VmScopedLockRetry(unittest.TestCase):
    """A per-VM `lock-<vmid>.conf` timeout on a VM-scoped task is retried.

    Live-found 2026-10-16 (crimson-relay's re-run, run-dd88db6d): phase 4's ACPI shutdown
    was abandoned at our own 120 s client timeout while its `qmshutdown` task was still
    running, and the immediate force-stop then lost `lock-2351.conf` to that same task. Two
    things are held shut here: `stop_vm` hands PVE its own shutdown timeout so the task ends
    on its own and releases the lock, and a stop that still loses the lock is retried (a
    stop is idempotent). A failure that is not a lock is surfaced at once, as before.
    """

    NODE, VMID = "proxmox", 2351

    @contextlib.contextmanager
    def _patched(self, api, wait, status="running"):
        with patch.object(client, "proxmox_api", api), \
             patch.object(client, "wait_for_proxmox_task", wait), \
             patch.object(client.time, "sleep", lambda *_: None), \
             patch.object(lifecycle, "vm_status", lambda *a: status), \
             contextlib.redirect_stdout(io.StringIO()):
            yield

    def _stopper(self, wait, status="running"):
        """(api, posts) where `posts` records every POST body the stop issues."""
        posts = []

        def api(method, path, **kw):
            posts.append((path.rsplit("/", 1)[-1], kw.get("data")))
            return {"data": "UPID:" + ("shutdown" if path.endswith("/shutdown") else "stop")}

        return api, posts, wait, status

    def test_shutdown_gets_pve_its_own_timeout_so_the_task_cannot_be_abandoned(self):
        waited = []

        def wait(node, upid, timeout=1800):
            waited.append(timeout)

        posts = []

        def api(method, path, **kw):
            posts.append(kw.get("data"))
            return {"data": "UPID:shutdown"}

        with self._patched(api, wait, status="stopped"):
            lifecycle.stop_vm(self.NODE, self.VMID)
        self.assertEqual(posts, [{"timeout": lifecycle.SHUTDOWN_GRACE_S}],
                         "PVE must be told to end its own shutdown task")
        self.assertGreater(waited[0], lifecycle.SHUTDOWN_GRACE_S,
                           "our wait must outlast PVE's, or we abandon a lock-holding task")

    def test_a_lock_failed_stop_is_retried(self):
        stops = []

        def api(method, path, **kw):
            if not path.endswith("/stop"):
                return {"data": "UPID:shutdown"}
            stops.append(path)
            return {"data": "UPID:stop"}

        def wait(node, upid, timeout=1800):
            if upid == "UPID:shutdown":
                raise RuntimeError("Proxmox task UPID:shutdown failed: VM still running")
            if len(stops) == 1:
                raise RuntimeError(VM_LOCK_MESSAGE)

        with self._patched(api, wait):
            lifecycle.stop_vm(self.NODE, self.VMID)
        self.assertEqual(len(stops), 2, "the lock-failed stop is retried, not fatal")

    def test_a_stop_that_keeps_losing_the_lock_is_bounded(self):
        stops = []

        def api(method, path, **kw):
            if not path.endswith("/stop"):
                return {"data": "UPID:shutdown"}
            stops.append(path)
            return {"data": "UPID:stop"}

        def wait(node, upid, timeout=1800):
            if upid == "UPID:shutdown":
                raise RuntimeError("Proxmox task UPID:shutdown failed: VM still running")
            raise RuntimeError(VM_LOCK_MESSAGE)

        with self._patched(api, wait):
            with self.assertRaises(RuntimeError) as ctx:
                lifecycle.stop_vm(self.NODE, self.VMID)
        self.assertIn("lock file", str(ctx.exception))
        self.assertEqual(len(stops), 5, "the retry is bounded")

    def test_a_non_lock_failure_is_not_retried(self):
        calls = []

        def api(method, path, **kw):
            calls.append(path)
            return {"data": "UPID:x"}

        def wait(node, upid, timeout=1800):
            raise RuntimeError("Proxmox task UPID:x failed: storage 'hdd' does not exist")

        with patch.object(client, "proxmox_api", api), \
             patch.object(client, "wait_for_proxmox_task", wait), \
             patch.object(client.time, "sleep", lambda *_: None):
            with self.assertRaises(RuntimeError):
                client.run_vm_task(self.NODE, self.VMID, "POST",
                                   f"/nodes/{self.NODE}/qemu/{self.VMID}/status/stop")
        self.assertEqual(len(calls), 1, "a real failure is surfaced, not retried")
