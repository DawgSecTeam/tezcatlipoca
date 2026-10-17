"""Proxmox API client: TLS-pinned requests, per-node route table (multi-node), cluster reads,
task waiting and node-load throttling. Everything else talks to Proxmox through here."""

import fcntl
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

import requests

from common.utils import retry
from estate import config
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

# Multi-node placement routes, registered by placement_record.activate_placement(): node
# name -> (endpoint, api_token). proxmox_api consults this table for node-scoped
# paths, so the ~50 existing call sites — which all pass the right per-node name —
# reach the owning host unchanged. Empty table = single-node behavior.
_NODE_ROUTES = {}
_NODE_PATH_RE = re.compile(r"^/nodes/([^/]+)(?:/|$)")


def register_node_routes(routes):
    _NODE_ROUTES.clear()
    _NODE_ROUTES.update(routes)


def clear_node_routes():
    _NODE_ROUTES.clear()


def node_routes_active():
    return bool(_NODE_ROUTES)


def _route_for_path(path):
    """(endpoint, token) override for a node-scoped path, or None (env endpoint)."""
    if _NODE_ROUTES:
        m = _NODE_PATH_RE.match(path)
        if m:
            return _NODE_ROUTES.get(m.group(1))
    return None


def live_vmids():
    """Every vmid the cluster currently knows, as a set.

    One cluster-level read serves a whole resume gate, and it works identically on a
    single node and across a multi-node placement (each host's own view lists its own
    VMs, and the gate only needs "does this vmid exist somewhere in this range").

    Raises RuntimeError rather than returning an empty set when the API cannot be
    reached: an empty set would read as "every machine is gone" and hard-fail a resume
    that is actually fine, which is its own kind of lie."""
    try:
        data = proxmox_api("GET", "/cluster/resources", params={"type": "vm"})["data"]
    except Exception as e:                                  # noqa: BLE001 - reported
        raise RuntimeError(f"could not list the cluster's VMs to verify the resume: {e}")
    return {int(v["vmid"]) for v in (data or []) if v.get("vmid") is not None}


class _FingerprintAdapter(HTTPAdapter):
    def __init__(self, fingerprint, **kw):
        self._fingerprint = fingerprint
        super().__init__(**kw)

    def init_poolmanager(self, connections, maxsize, block=False, **kw):
        self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block,
                                       assert_fingerprint=self._fingerprint, **kw)


def proxmox_request(method, url, fingerprint=None, **kwargs):
    """requests wrapper honoring optional TLS pinning. config.proxmox.tls_fingerprint pins the
    cert; config.proxmox.ca_bundle verifies against a CA path; neither set => verify=False (lab
    default). An explicit `fingerprint` (per-node record) overrides the configured value."""
    cfg = config.active()
    fp = fingerprint or cfg.proxmox.tls_fingerprint
    ca = cfg.proxmox.ca_bundle
    session = requests.Session()
    if fp:
        session.mount("https://", _FingerprintAdapter(fp))
        kwargs.setdefault("verify", False)
    else:
        kwargs.setdefault("verify", ca or False)
    return session.request(method, url, **kwargs)


def proxmox_api_for(endpoint, token, method, path, **kwargs):
    """Parameterized core of proxmox_api: explicit endpoint+token instead of env.
    Used by the multi-node placement probes and by proxmox_api's route overrides."""
    url = f"{endpoint.rstrip('/')}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={token}"}

    def _call():
        # A caller's own timeout wins. Passing both it and a default raised "got multiple
        # values for keyword argument 'timeout'" — which list_proxmox_templates swallowed
        # into an empty list, so the templates picker read "0 of 17 live" on every call
        # .
        kwargs.setdefault("timeout", 60)
        r = proxmox_request(method, url, headers=headers, **kwargs)
        r.raise_for_status()
        return r.json()

    def _retry_on(exc):
        # PVE serializes API writes against its per-VM (`/var/lock/qemu-server/lock-N.conf`)
        # and per-datastore (`pve-storage-*`) lock files, and answers the loser of a race
        # with a 500 "can't lock file ... - got timeout" while the winner's operation is
        # still running. With concurrent ranges that is routine, not exceptional — three
        # parallel deploys hit it on a config PUT in phase 4 (caught live,
        # on the run that found it: golden vmid 2350). The request is idempotent and the lock is held
        # only for the duration of one operation, so the same bounded retry that already
        # covers connection errors applies.
        if not isinstance(exc, (requests.exceptions.ConnectionError,
                                requests.exceptions.Timeout,
                                requests.exceptions.HTTPError)):
            return False
        response = getattr(exc, "response", None)
        return response is not None and "can't lock file" in (response.text or "")

    return retry(_call, attempts=4, gap=2, backoff=True,
                 retry_on=_retry_on)


def proxmox_api(method, path, **kwargs):
    override = _route_for_path(path)
    if override:
        endpoint, token = override
    else:
        cfg = config.active()
        endpoint = cfg.require("proxmox.endpoint")
        token = cfg.require("proxmox.api_token")
    return proxmox_api_for(endpoint, token, method, path, **kwargs)


def cluster_vms_for(node):
    """Cluster-wide VM list as seen from the host that owns `node`.

    Single-node: the env endpoint. With a multi-node placement active: the owning
    node's own endpoint — on independent hosts its /cluster/resources sees only its
    own VMs, so per-node template maps (golden_target_ops._template_vmid_map, preflight)
    must ask the right host. The node filter in the callers stays harmless either
    way (a one-node cluster view always matches)."""
    override = _route_for_path(f"/nodes/{node}/x")
    if override:
        endpoint, token = override
        return proxmox_api_for(endpoint, token, "GET", "/cluster/resources",
                               params={"type": "vm"})["data"]
    return proxmox_api("GET", "/cluster/resources", params={"type": "vm"})["data"]


def wait_for_proxmox_task(node, upid, timeout=1800):
    deadline = time.time() + timeout
    while True:
        if time.time() > deadline:
            raise RuntimeError(f"Proxmox task {upid} timed out after {timeout}s")
        data = proxmox_api("GET", f"/nodes/{node}/tasks/{upid}/status")["data"]
        status, exitstatus = data.get("status") or "", data.get("exitstatus") or ""
        # A task whose worker died carries the failure IN `status` ("clone failed: can't
        # lock file … - got timeout") and has no `exitstatus` at all, so a `== "stopped"`
        # test never fires and the caller polls a corpse to its 1800 s timeout
        # (caught live: two concurrent deploys sat 30 min on a dead qmclone
        # while the third died on the same task in 7 s). Only "running" is not-terminal.
        if status == "running":
            time.sleep(3)
            continue
        # "WARNINGS: n" is PVE's completed-with-warnings exit — the task's work
        # is done (caught live: qmdestroy of a team clone whose
        # cloud-init volume was already gone exits WARNINGS, VM verifiably
        # destroyed). An empty exit is "still being written", not a failure.
        if status == "stopped" and exitstatus in ("", "OK"):
            return
        if exitstatus.startswith("WARNINGS"):
            return
        raise RuntimeError(
            f"Proxmox task {upid} failed: {exitstatus or status or 'unknown status'}")


def run_vm_task(node, vmid, method, path, *, timeout=1800, attempts=5, **kwargs):
    """Issue a VM-scoped PVE operation and wait for its task, retrying one PVE refused for
    `lock-<vmid>.conf` contention.

    PVE holds that per-VM lock for a task's whole duration, so two shapes collide here: a
    VM-scoped task racing another already in flight, and — the failure this exists for — a
    caller that gave up on a still-running task and then issues the next operation against
    the lock it still holds (a 120 s client timeout abandoning `qmshutdown` and the
    immediate force-stop that lost `lock-<vmid>.conf` to it, on the run that found this).
    `lifecycle.stop_vm` no longer abandons its shutdown task;
    this absorbs the contention that remains (a foreign operation, or a slow release). Every
    operation routed here is idempotent, so the retry is safe, and a failure that does not
    name a lock file is raised at once. Same 40 s-ramp as the datastore clone retry.
    """
    for attempt in range(attempts):
        upid = proxmox_api(method, path, **kwargs)["data"]
        try:
            wait_for_proxmox_task(node, upid, timeout=timeout)
            return
        except RuntimeError as e:
            if "lock file" not in str(e) or attempt == attempts - 1:
                raise
            print(f"    vmid {vmid} lost the per-VM lock — retrying "
                  f"(attempt {attempt + 2}/{attempts})")
            time.sleep(40 * (attempt + 1))


def node_bridges(node):
    """{iface: comment} for every bridge the node owns — the only identity a bridge has."""
    nets = proxmox_api("GET", f"/nodes/{node}/network")["data"]
    return {n.get("iface"): str(n.get("comments") or "")
            for n in nets if n.get("iface") and n.get("type") in (None, "bridge")}


def destroy_bridge_if_exists(node, bridge_name):
    """Delete a Proxmox Linux bridge if it exists; True when it is gone afterwards.

    A GET-first existence check keeps a fresh deploy (whose bridges are all new) from
    spraying spurious 400s — PVE rejects DELETE with "Parameter verification failed"
    rather than 404 for an absent iface (observed live).
    DELETE only STAGES the change — the same pending/apply pair create_bridge pairs with a
    reload PUT — so without the apply the bridge stays UP and the next deploy's preflight
    refuses it (caught live: left vmbr151 active through a teardown that
    reported the estate clean).
    """
    try:
        existing = {n.get("iface") for n in proxmox_api("GET", f"/nodes/{node}/network")["data"]}
        if bridge_name not in existing:
            return True
        proxmox_api("DELETE", f"/nodes/{node}/network/{bridge_name}")
        proxmox_api("PUT", f"/nodes/{node}/network")
        remaining = {n.get("iface") for n in proxmox_api("GET", f"/nodes/{node}/network")["data"]}
        return bridge_name not in remaining
    except requests.exceptions.HTTPError as e:
        if e.response is None or e.response.status_code != 404:
            print(f"    WARNING: could not delete bridge {bridge_name}: {e}")
        return False
    except Exception as e:
        print(f"    WARNING: could not delete bridge {bridge_name}: {e}")
        return False


@contextmanager
def storage_queue_lock(node, wait_s=900):
# Serialise this host's first-time storage-heavy PVE operations (base-image clone, golden ->
#     template conversion).
#
#     PVE takes a per-DATASTORE lock for the duration of either and reports `can't lock file
#     '/var/lock/pve-manager/pve-storage-hdd' - got timeout` to everyone else, so concurrent
#     ranges do not queue — they fight, and the loser spends its retry budget or fails outright.
#     caught live: the second three-range campaign: three same-image clones contending left
#     the loser on its fourth attempt, and a golden's template conversion hit the same lock and
#     failed, leaving `template: 1` with the volume never renamed to `base-` (linked clones would
#     have died with the opaque 500 the volume guard now names).
#
#     Retrying harder is a worse answer than not racing, so our own deploys queue here. Keyed on
#     the node rather than the datastore because the datastores in play are node-local and the
#     conservative superset is the one that cannot contend: over-serialising two operations that
#     together cost seconds is cheaper than diagnosing a lost conversion. Contention we do not
#     control (another host, or a manual run on older code) is left to the callers' retries.
#
#     Bounded on purpose: a holder that is alive but wedged must not block a deploy forever.
    endpoint = (config.active().proxmox.endpoint or "unknown")
    host = re.sub(r"^https?://", "", endpoint).split("/")[0].split(":")[0] or "unknown"
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", f"{host}-{node}")
    path = Path.home() / ".tezcatlipoca" / "locks" / f"storage-{slug}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        deadline, announced = time.time() + wait_s, False
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    print(f"    storage queue lock held >{wait_s}s — proceeding anyway; the "
                          f"operation's own retry absorbs the contention")
                    break
                if not announced:
                    holder = (path.read_text().strip() or "?")
                    print(f"    waiting for the storage queue (held by pid {holder}) — queuing "
                          f"instead of racing for the datastore lock")
                    announced = True
                time.sleep(0.5)
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()
