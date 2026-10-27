"""Proxmox API client: TLS-pinned requests, per-node routes, cluster reads, task waits."""

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

# Registered by placement_record.activate_placement(); node-scoped paths resolve to the owning host.
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
    if _NODE_ROUTES:
        m = _NODE_PATH_RE.match(path)
        if m:
            return _NODE_ROUTES.get(m.group(1))
    return None


def live_vmids():
    """Every vmid visible from every host this process routes to.

    Raises on failure: an empty set reads as "everything is gone" and kills a fine resume.
    Independent Proxmox hosts only list their OWN VMs in /cluster/resources, so a multi-node
    resume must ask each routed host or every satellite VM reads as gone.
    """
    hosts = sorted(_NODE_ROUTES) or [None]
    vmids = set()
    for host in hosts:
        try:
            if host is None:
                data = proxmox_api("GET", "/cluster/resources", params={"type": "vm"})["data"]
            else:
                data = cluster_vms_for(host)
        except Exception as e:                              # noqa: BLE001 - reported
            raise RuntimeError(f"could not list the cluster's VMs to verify the resume: {e}")
        vmids |= {int(v["vmid"]) for v in (data or []) if v.get("vmid") is not None}
    return vmids


class _FingerprintAdapter(HTTPAdapter):
    def __init__(self, fingerprint, **kw):
        self._fingerprint = fingerprint
        super().__init__(**kw)

    def init_poolmanager(self, connections, maxsize, block=False, **kw):
        self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block,
                                       assert_fingerprint=self._fingerprint, **kw)


def proxmox_request(method, url, fingerprint=None, **kwargs):
    cfg = config.active()
    fp = fingerprint or cfg.proxmox.tls_fingerprint
    ca = cfg.proxmox.ca_bundle
    with requests.Session() as session:
        if fp:
            session.mount("https://", _FingerprintAdapter(fp))
            kwargs.setdefault("verify", False)
        else:
            kwargs.setdefault("verify", ca or False)
        return session.request(method, url, **kwargs)


def proxmox_api_for(endpoint, token, method, path, fingerprint=None, **kwargs):
    url = f"{endpoint.rstrip('/')}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={token}"}
    # Only re-send a request the server cannot have applied twice: a POST that timed out may
    # still be running (a re-POSTed /clone fails "already exists"), so it is sent once.
    idempotent = method in ("GET", "HEAD", "PUT", "DELETE")

    def _call():
        kwargs.setdefault("timeout", 60)
        r = proxmox_request(method, url, fingerprint=fingerprint, headers=headers, **kwargs)
        r.raise_for_status()
        return r.json()

    def _retry_on(exc):
        # PVE serializes API writes on per-VM/datastore lock files and answers the loser
        # with a 500 "can't lock file"; the calls are idempotent, so retry is safe.
        if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return idempotent
        if not isinstance(exc, requests.exceptions.HTTPError):
            return False
        response = getattr(exc, "response", None)
        return response is not None and "can't lock file" in (response.text or "")

    return retry(_call, attempts=4, gap=2, backoff=True,
                 retry_on=_retry_on)


def proxmox_api(method, path, **kwargs):
    override = _route_for_path(path)
    if override:
        endpoint, token, fingerprint = override
        kwargs.setdefault("fingerprint", fingerprint)
    else:
        cfg = config.active()
        endpoint = cfg.require("proxmox.endpoint")
        token = cfg.require("proxmox.api_token")
    return proxmox_api_for(endpoint, token, method, path, **kwargs)


def cluster_vms_for(node):
    """Independent hosts' /cluster/resources shows only their own VMs — ask the owning host."""
    override = _route_for_path(f"/nodes/{node}/x")
    if override:
        endpoint, token, fingerprint = override
        return proxmox_api_for(endpoint, token, "GET", "/cluster/resources",
                               fingerprint=fingerprint, params={"type": "vm"})["data"]
    return proxmox_api("GET", "/cluster/resources", params={"type": "vm"})["data"]


def wait_for_proxmox_task(node, upid, timeout=1800):
    deadline = time.time() + timeout
    while True:
        if time.time() > deadline:
            raise RuntimeError(f"Proxmox task {upid} timed out after {timeout}s")
        data = proxmox_api("GET", f"/nodes/{node}/tasks/{upid}/status")["data"]
        status, exitstatus = data.get("status") or "", data.get("exitstatus") or ""
        # A dead worker's failure is in status with no exitstatus; only "running" is not terminal.
        if status == "running":
            time.sleep(3)
            continue
        # exitstatus "WARNINGS: n" = done with warnings; empty = still being written, not failure.
        if status == "stopped" and exitstatus in ("", "OK"):
            return
        if exitstatus.startswith("WARNINGS"):
            return
        raise RuntimeError(
            f"Proxmox task {upid} failed: {exitstatus or status or 'unknown status'}")


def run_vm_task(node, vmid, method, path, *, timeout=1800, attempts=5, **kwargs):
    """Retries PVE lock-contention refusals; every operation routed here is idempotent."""
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
    """A bridge's comment is its only identity in PVE."""
    nets = proxmox_api("GET", f"/nodes/{node}/network")["data"]
    return {n.get("iface"): str(n.get("comments") or "")
            for n in nets if n.get("iface") and n.get("type") in (None, "bridge")}


def destroy_bridge_if_exists(node, bridge_name):
    """PVE DELETE only stages network changes; the apply PUT is what takes the bridge down."""
    try:
        existing = {n.get("iface") for n in proxmox_api("GET", f"/nodes/{node}/network")["data"]}
        if bridge_name not in existing:
            return True
        proxmox_api("DELETE", f"/nodes/{node}/network/{bridge_name}")
        upid = proxmox_api("PUT", f"/nodes/{node}/network").get("data")
        if upid:
            wait_for_proxmox_task(node, upid)
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
    # Concurrent base-image clones/conversions hit PVE datastore lock timeouts; queue, don't race.
    endpoint = (config.active().proxmox.endpoint or "unknown")
    host = re.sub(r"^https?://", "", endpoint).split("/")[0].split(":")[0] or "unknown"
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", f"{host}-{node}")
    path = Path.home() / ".tezcatlipoca" / "locks" / f"storage-{slug}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    # `a+`: truncating before the flock wipes the holder's pid and makes the wait message lie.
    fh = open(path, "a+")
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
