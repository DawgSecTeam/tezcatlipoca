"""Proxmox API client: TLS-pinned requests, per-node route table (multi-node), cluster reads,
task waiting and node-load throttling. Everything else talks to Proxmox through here."""

import os
import re
import time

import requests

from utils import retry
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
    """requests wrapper honoring optional TLS pinning. PROXMOX_TLS_FINGERPRINT (sha256) pins the
    cert; PROXMOX_CA_BUNDLE verifies against a CA path; neither set => verify=False (lab default).
    An explicit `fingerprint` (per-node record) overrides the env value."""
    fp = fingerprint or os.environ.get("PROXMOX_TLS_FINGERPRINT")
    ca = os.environ.get("PROXMOX_CA_BUNDLE")
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
        r = proxmox_request(method, url, headers=headers, timeout=60, **kwargs)
        r.raise_for_status()
        return r.json()

    return retry(_call, attempts=4, gap=2, backoff=True,
                 retry_on=(requests.exceptions.ConnectionError, requests.exceptions.Timeout))


def proxmox_api(method, path, **kwargs):
    override = _route_for_path(path)
    if override:
        endpoint, token = override
    else:
        endpoint = os.environ["TF_VAR_proxmox_endpoint"]
        token = os.environ["TF_VAR_proxmox_api_token"]
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
        if data["status"] == "stopped":
            exitstatus = data.get("exitstatus") or ""
            # "WARNINGS: n" is PVE's completed-with-warnings exit — the task's work
            # is done (live-found 2026-09-26: qmdestroy of a team clone whose
            # cloud-init volume was already gone exits WARNINGS, VM verifiably
            # destroyed). Only anything else is a failure.
            if exitstatus != "OK" and not exitstatus.startswith("WARNINGS"):
                raise RuntimeError(f"Proxmox task {upid} failed: {exitstatus}")
            return
        time.sleep(3)


def destroy_bridge_if_exists(node, bridge_name):
    """Delete a Proxmox Linux bridge if it exists. A GET-first existence check keeps
    a fresh deploy (whose bridges are all new) from spraying spurious 400s — PVE
    rejects DELETE with "Parameter verification failed" rather than 404 for an
    absent iface (live noise, m4-validation-2026-09-25 phase 1)."""
    try:
        existing = {n.get("iface") for n in proxmox_api("GET", f"/nodes/{node}/network")["data"]}
        if bridge_name not in existing:
            return
        proxmox_api("DELETE", f"/nodes/{node}/network/{bridge_name}")
    except requests.exceptions.HTTPError as e:
        if e.response is None or e.response.status_code != 404:
            print(f"    WARNING: could not delete bridge {bridge_name}: {e}")
    except Exception as e:
        print(f"    WARNING: could not delete bridge {bridge_name}: {e}")
