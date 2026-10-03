"""Proxmox API layer, VM identity math, and (team, box) target abstraction."""

import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import NamedTuple

from utils import record_degradation

import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

# NOTE: range_ops is NOT a constants shim. It used to import MAX_TEAMS,
# SNAP_BASE, SNAP_READY, ... purely so that redeploy-competition.py could pull
# them through `from range_ops import (...)`. Those five were unused here, so
# every cleanup pass flagged them — and deleting them would have broken
# redeploy-competition.py at import time. Constants now come from constants.py
# directly (w3 audit 2026-10-01); do not re-add re-exports.

# Multi-node placement routes, registered by nodes_ops.activate_placement(): node
# name -> (endpoint, api_token). proxmox_api consults this table for node-scoped
# paths, so the ~50 existing call sites — which all pass the right per-node name —
# reach the owning host unchanged. Empty table = single-node legacy behavior.
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

REPO_ROOT = Path(__file__).resolve().parent
_TF_TEMPLATE_DIR = REPO_ROOT / "terraform"
# Only the .tf sources + provider lock are per-competition template files; the
# state, .terraform plugin dir, and terraform.tfvars.json live locally per comp.
_TF_TEMPLATE_FILES = ("main.tf", "variables.tf", "outputs.tf", ".terraform.lock.hcl")


def terraform_dir(comp_dir):
    """Per-competition Terraform working dir (its own state + lock), so two
    competitions can `terraform apply` concurrently on one node instead of
    contending on the single shared terraform/terraform.tfstate."""
    return Path(comp_dir) / "terraform"


def ensure_terraform_workdir(comp_dir):
    """Materialize competitions/<id>/terraform/ from the canonical terraform/
    template: (re)copy the .tf sources + provider lock, never touching the local
    tfstate/.terraform/terraform.tfvars.json. Idempotent."""
    dst = terraform_dir(comp_dir)
    dst.mkdir(parents=True, exist_ok=True)
    for name in _TF_TEMPLATE_FILES:
        src = _TF_TEMPLATE_DIR / name
        if src.exists():
            shutil.copy2(src, dst / name)
    return dst


def terraform_plugin_cache_dir():
    """Shared provider-plugin cache so each per-comp `terraform init` links the
    provider from disk instead of re-downloading it."""
    cache = _TF_TEMPLATE_DIR / ".terraform-plugin-cache"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


class _FingerprintAdapter(HTTPAdapter):
    def __init__(self, fingerprint, **kw):
        self._fingerprint = fingerprint
        super().__init__(**kw)

    def init_poolmanager(self, connections, maxsize, block=False, **kw):
        self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block,
                                       assert_fingerprint=self._fingerprint, **kw)


def proxmox_request(method, url, **kwargs):
    """requests wrapper honoring optional TLS pinning. PROXMOX_TLS_FINGERPRINT (sha256) pins the
    cert; PROXMOX_CA_BUNDLE verifies against a CA path; neither set => verify=False (lab default)."""
    fingerprint = os.environ.get("PROXMOX_TLS_FINGERPRINT")
    ca = os.environ.get("PROXMOX_CA_BUNDLE")
    session = requests.Session()
    if fingerprint:
        session.mount("https://", _FingerprintAdapter(fingerprint))
        kwargs.setdefault("verify", False)
    else:
        kwargs.setdefault("verify", ca or False)
    return session.request(method, url, **kwargs)


def proxmox_api_for(endpoint, token, method, path, **kwargs):
    """Parameterized core of proxmox_api: explicit endpoint+token instead of env.
    Used by the multi-node placement probes and by proxmox_api's route overrides."""
    url = f"{endpoint.rstrip('/')}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={token}"}
    last_exc = None
    for attempt in range(4):
        try:
            r = proxmox_request(method, url, headers=headers, timeout=60, **kwargs)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_exc = e
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
    raise last_exc


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
    own VMs, so per-node template maps (golden_ops._template_vmid_map, preflight)
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


def diagnose_unreachable_box(node, vmid):
    """Diagnose unreachable box via guest agent (virtio-serial, no network needed). Never raises."""

    try:
        ifaces = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
        )["data"]["result"]
        addrs = [
            a["ip-address"] for iface in ifaces for a in iface.get("ip-addresses", [])
            if a.get("ip-address-type") == "ipv4" and not a["ip-address"].startswith("127.")
        ]
        iface_summary = f"has IPv4 {', '.join(addrs)}" if addrs else "has NO IPv4 address on any interface"
    except Exception as e:
        return f"      (guest agent unreachable for vmid {vmid}, can't diagnose further: {e})"

    cloud_init_summary = "(cloud-init status unavailable)"
    try:
        pid = proxmox_api(
            "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
            data={"command": ["cloud-init", "status", "--long"]},
        )["data"]["pid"]
        deadline = time.time() + 10
        while time.time() < deadline:
            s = proxmox_api(
                "GET", f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
                params={"pid": pid},
            )["data"]
            if s.get("exited"):
                out = (s.get("out-data") or "").strip()
                cloud_init_summary = out.splitlines()[0] if out else "(no output)"
                break
            time.sleep(0.5)
    except Exception:
        pass

    return f"      guest agent (vmid {vmid}): {iface_summary}; cloud-init {cloud_init_summary}"


def guest_agent_exec_root(node, vmid, script, timeout=60, shell="bash"):
    """Run a shell script as root via the QEMU guest agent; returns (exit_code, stdout,
    stderr). shell="sh" for guests without bash (the alpine jump VM)."""
    pid = proxmox_api(
        "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
        data={"command": [shell, "-c", script]},
    )["data"]["pid"]
    last = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
            params={"pid": pid},
        )["data"]
        if s.get("exited"):
            return s.get("exitcode", -1), s.get("out-data", ""), s.get("err-data", "")
        last = s
        time.sleep(1)
    raise RuntimeError(_exec_timeout_message(vmid, pid, timeout, last))


def guest_agent_exec_windows(node, vmid, ps_script, timeout=120):
    """Run PowerShell as SYSTEM via the guest agent; only channel before Windows bootstrap."""
    import base64
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    pid = proxmox_api(
        "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
        data={"command": [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
        ]},
    )["data"]["pid"]
    last = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
            params={"pid": pid},
        )["data"]
        if s.get("exited"):
            return s.get("exitcode", -1), s.get("out-data", ""), s.get("err-data", "")
        last = s
        time.sleep(1)
    raise RuntimeError(_exec_timeout_message(vmid, pid, timeout, last))


def _exec_timeout_message(vmid, pid, timeout, status):
    """Explain a guest-exec timeout instead of just naming it.

    A bare "didn't finish within Ns" gives an operator nothing to act on, and five
    exec logs carry exactly that at the 120s cap (live-found 2026-09-26..10-01). The
    agent already holds the partial output by the time we give up — include it, and
    point at the detached path for work that legitimately outlives a poll budget.
    """
    status = status or {}
    detail = []
    for label, key in (("stdout", "out-data"), ("stderr", "err-data")):
        text = str(status.get(key) or "").strip()
        if text:
            detail.append(f"{label} so far: {text[-400:]!r}")
    tail = ("; " + "; ".join(detail)) if detail else ""
    return (f"guest-agent exec pid={pid} on vmid {vmid} did not finish within {timeout}s"
            f"{tail}. For work that can outlive the budget, call "
            f"guest_agent_exec_detached() (nohup + log polling) rather than raising "
            f"the timeout.")


class DetachedExecResult(NamedTuple):
    """`rc` is the script's exit status; `log` is the tail of the guest-side log;
    `log_path` is where that log lives ON THE GUEST (not stderr — a detached run
    has one merged stream, and the file outlives the call)."""

    rc: int
    log: str
    log_path: str


# Written by the same shell that ran the payload, so it cannot appear in the log
# before the payload has finished.
DETACHED_RC_MARKER = "__TZ_DETACHED_RC="


def guest_agent_exec_detached(node, vmid, script, log_path, timeout=1800,
                              shell="bash", poll_interval=5):
    """Run a long root script via the guest agent without holding the exec channel open.

    The agent's exec channel is a poll loop with a caller-set budget, so work that
    legitimately outlives that budget (apt installs, nakon bundles, service fixups)
    used to die mid-plant with a timeout — and the plant's partial effects stayed on
    the box. This starts the payload under setsid+nohup, redirects it to a log on the
    guest, and polls that log for a completion marker, so `timeout` is a real deadline
    rather than a request/response budget.

    Returns DetachedExecResult(rc, log_tail, log_path). The log survives a dropped
    agent channel, so a timeout here is diagnosable after the fact on the guest.
    """
    qlog = shlex.quote(log_path)
    # A subshell keeps an `exit` inside the payload from skipping the rc marker, and
    # the marker is written by the same shell that ran the payload — so it cannot
    # appear in the log before the payload has finished.
    body = (f"( {script}\n); __tz_rc=$?; "
            f'printf "%s%s\\n" "{DETACHED_RC_MARKER}" "$__tz_rc" >> {qlog}')
    wrapper = (f"rm -f {qlog}; "
               f"setsid nohup {shell} -c {shlex.quote(body)} "
               f"> {qlog} 2>&1 < /dev/null & echo started")
    proxmox_api(
        "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
        data={"command": [shell, "-c", wrapper]},
    )

    deadline = time.time() + timeout
    last_log = ""
    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            _rc, out, _err = guest_agent_exec_root(
                node, vmid, f"tail -c 4000 {qlog} 2>/dev/null || true", timeout=30)
        except Exception:
            # A transient agent hiccup is expected while the payload churns; the log
            # on the guest is the durable record, so keep polling until the deadline.
            continue
        last_log = out or ""
        marker_at = last_log.rfind(DETACHED_RC_MARKER)
        if marker_at != -1:
            tail = last_log[marker_at + len(DETACHED_RC_MARKER):].strip()
            rc_text = tail.splitlines()[0].strip() if tail else ""
            try:
                rc = int(rc_text)
            except ValueError:
                rc = -1
            return DetachedExecResult(rc, last_log, log_path)
    raise RuntimeError(
        f"guest-agent detached exec on vmid {vmid} did not finish within {timeout}s "
        f"(log on the guest: {log_path}). Last log tail: {last_log[-400:]!r}")


def wait_for_guest_agent(node, vmid, timeout=300):
    """Wait for guest agent ping (virtio-serial, no network needed). Never raises."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/agent/ping", data={})
            return True
        except Exception:
            pass
        time.sleep(5)
    # Never raises — callers depend on the boolean — but a silent False is how a
    # 45-minute escalation looks like progress (observed 600 -> 1200 -> 2700 -> 2900s
    # for one vmid). Say what the VM is actually doing instead.
    print(f"    guest agent on vmid {vmid} did not answer within {timeout}s{_vm_state_suffix(node, vmid)}")
    return False


def _vm_state_suffix(node, vmid):
    """Best-effort ' (status=running, name=…)' for a failed agent wait. Never raises."""
    try:
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
        status = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/status/current")["data"]
        return (f" (status={status.get('status', '?')}, name={cfg.get('name', '?')}, "
                f"lock={cfg.get('lock') or 'none'})")
    except Exception as e:
        return f" (state unavailable: {e})"


def node_loadavg(node):
    """1-minute load average of a node, or None when the node won't say. Never raises."""
    try:
        data = proxmox_api("GET", f"/nodes/{node}/status")["data"]
        loadavg = data.get("loadavg") or []
        return float(loadavg[0]) if loadavg else None
    except Exception:
        return None


def wait_for_node_load(node, max_load, timeout=3600, poll=30, sleep=time.sleep):
    """Block until the node's 1-minute load drops below `max_load`. Returns True if it did.

    Phase-4 golden retries were hand-throttled by an operator watching load
    ("load=32.06 (attempt 1/36) … load below 10 — launching phase-4 resume",
    2026-09-30, eight consecutive failures on one Windows golden while a second
    deploy saturated the host). Raising the sysprep budget 900s -> 1800s did not fix
    it; the contention did. A node that never reports load, or never comes down
    within `timeout`, returns False — the caller decides whether to proceed anyway,
    because the load may be this very deploy.

    `sleep` is injectable so the offline tests do not wait a real hour.
    """
    deadline = time.time() + timeout
    while True:
        load = node_loadavg(node)
        if load is None:
            print(f"    node {node} did not report a load average — not waiting")
            return False
        if load < max_load:
            print(f"    node {node} load {load:.2f} < {max_load} — proceeding")
            return True
        if time.time() >= deadline:
            print(f"    node {node} load still {load:.2f} (>= {max_load}) after "
                  f"{timeout}s — proceeding anyway")
            return False
        print(f"    node {node} load {load:.2f} >= {max_load} — waiting {poll}s")
        sleep(poll)




def vm_id_for(identifier, box_index):
    return 200 + int(identifier) * 10 + box_index


def box_index(boxes, box_name):
    """Canonical position of a box by name — matches terraform's index(names, name)."""
    for i, b in enumerate(boxes):
        if b["name"] == box_name:
            return i
    raise KeyError(f"box {box_name!r} not in boxes list")


def persist_targets(comp_dir, targets, boxes):
    """Freeze (team,box)->vmid at deploy so later tools don't recompute it from boxes.json order."""
    data = {
        "box_order": [b["name"] for b in boxes],
        "targets": {
            t["vm_name"]: {
                "team_key": t["team_key"],
                "identifier": t["identifier"],
                "box_name": t["box_name"],
                "vmid": t["vmid"],
                "ip": t["ip"],
                **({"node": t["node"]} if t.get("node") else {}),
            }
            for t in targets
        },
    }
    (Path(comp_dir) / "targets.json").write_text(json.dumps(data, indent=2))


def load_targets(comp_dir, teams, boxes):
    """Targets with vmid read from targets.json (frozen at deploy); recompute if absent.

    Refuses when boxes.json was reordered since deploy: vmid is positional, so a
    reorder would silently retarget a different VM (and terraform would diverge too)."""
    path = Path(comp_dir) / "targets.json"
    if not path.exists():
        print("  (targets.json absent — deriving vmids from boxes.json order; "
              "reorder-unsafe for ranges deployed before this was added)")
        return enumerate_targets(teams, boxes)
    data = json.loads(path.read_text())
    current = [b["name"] for b in boxes]
    if data.get("box_order") != current:
        raise SystemExit(
            f"  ERROR: boxes.json box order changed since deploy (was {data.get('box_order')}, "
            f"now {current}). VM identity (vmid) is positional — restore the original order in "
            "boxes.json before redeploy/verify, or tear down and redeploy from scratch."
        )
    frozen = data.get("targets", {})
    result = []
    for t in enumerate_targets(teams, boxes):
        f = frozen.get(t["vm_name"])
        if f is not None:
            t = {**t, "vmid": f["vmid"], "ip": f.get("ip", t["ip"])}
        result.append(t)
    return result



def vm_status(node, vmid):
    """Current status string ('running'/'stopped'/...) or None if the VM doesn't exist."""
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    vm = next((v for v in vms if v["vmid"] == vmid), None)
    return vm.get("status") if vm else None


def stop_vm(node, vmid):
    try:
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/shutdown")["data"]
        wait_for_proxmox_task(node, upid, timeout=120)
    except RuntimeError:
        print(f"    vmid {vmid} ignored ACPI shutdown — forcing stop")
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
        wait_for_proxmox_task(node, upid, timeout=120)


def start_vm(node, vmid, timeout=120):
    """Start the VM if it isn't already running. No-op for a VM that doesn't exist."""
    status = vm_status(node, vmid)
    if status is None or status == "running":
        return
    upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/start")["data"]
    wait_for_proxmox_task(node, upid, timeout=timeout)


def clone_marker(comp_name):
    """Ownership marker written as the clone's `description` IN the clone POST itself.
    /clone takes no `tags`, and the tagging PUT only lands after the clone task finishes —
    a host reboot or kill mid-clone used to leave an untagged, clone-locked VM in our own
    slot that preflight called "foreign" and blocked every later deploy (winad-testrun
    2026-09-25). The description exists from the first instant of the clone."""
    return f"tezcatlipoca-clone comp-{comp_name}"


def has_clone_marker(node, vmid, comp_name):
    try:
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    except Exception:
        return False
    return clone_marker(comp_name) in str(cfg.get("description") or "")


def unlock_vm(node, vmid, lock):
    """Clear a stale lock (an interrupted clone's 'clone'). PVE only lets root@pam touch
    `lock`; with a token we fail loudly with the exact command instead of 'foreign VM'."""
    try:
        proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config", data={"delete": "lock"})
        print(f"    vmid {vmid}: cleared stale '{lock}' lock (interrupted clone)")
    except Exception as e:
        raise RuntimeError(
            f"vmid {vmid} is stuck with lock '{lock}' from an interrupted clone and this API "
            f"token cannot clear it ({e}). On the node run:  qm unlock {vmid} && qm destroy "
            f"{vmid} --destroy-unreferenced-disks 1 --purge 1   then re-run the deploy.") from e


def gc_orphan_volumes(node, vmid):
    """Delete disk volumes named for `vmid` when no VM with that vmid exists. An interrupted
    clone/destroy can strand vm-<vmid>-disk-N zvols; the next clone into the slot then fails
    with 'already exists'. Only called for vmids we just verified are empty and ours by
    computed slot, so every volume here is a leftover of our own."""
    removed = 0
    for st in proxmox_api("GET", f"/nodes/{node}/storage", params={"content": "images"})["data"]:
        try:
            vols = proxmox_api("GET", f"/nodes/{node}/storage/{st['storage']}/content",
                               params={"vmid": vmid})["data"]
        except Exception:
            continue
        for v in vols:
            if int(v.get("vmid") or -1) != vmid:
                continue
            upid = proxmox_api("DELETE", f"/nodes/{node}/storage/{st['storage']}/content/{v['volid']}")["data"]
            if upid:
                wait_for_proxmox_task(node, upid)
            removed += 1
    if removed:
        print(f"    vmid {vmid}: removed {removed} orphaned disk volume(s) (no VM owns them)")
    return removed


def destroy_vm_if_exists(node, vmid, expect_tags=None, legacy_name=None, allow_untagged=False):
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    vm = next((v for v in vms if v["vmid"] == vmid), None)
    if vm is None:
        gc_orphan_volumes(node, vmid)
        return
    if expect_tags is not None:
        # Defense in depth for the parallel cleanup sweep: a VM that carries tags without
        # ours is not ours, whatever the vmid math says (the preflight vmid-clash gate is
        # the other half of the ownership proof). The expected set is the FULL ownership
        # set (constants.ownership_tags): comp tag + per-deploy run tag — a same-comp VM
        # without this run's tag belongs to a DIFFERENT worktree's run (2026-10-02
        # near-miss) and is refused, not reclaimed.
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
        raw = str(cfg.get("tags") or "")
        # PVE joins tags with ';' in some views and ',' in others — accept both.
        tags = {t.strip() for t in raw.replace(";", ",").split(",") if t.strip()}
        missing = set(expect_tags) - tags
        if not tags:
            # Untagged VMs used to be destroyed on computed-vmid ownership alone; a
            # human-made VM parked in our vmid range died that way silently. Ownership
            # must be PROVEN now — the escape hatch is explicit (destroy's
            # --allow-untagged, phase 1's TEZ_ALLOW_UNTAGGED_RECLAIM=1) for the
            # pre-tagging era's ranges.
            if allow_untagged:
                print(f"    vmid {vmid} untagged — destroying on computed-vmid ownership "
                      f"(explicitly allowed)")
            else:
                raise RuntimeError(
                    f"refusing to destroy UNTAGGED vmid {vmid} ({vm.get('name')}): ownership "
                    f"cannot be proven. If this is a pre-run-tagging leftover of this "
                    f"competition, re-run with the explicit escape hatch "
                    f"(destroy-competition.py --allow-untagged, or "
                    f"TEZ_ALLOW_UNTAGGED_RECLAIM=1 for deploy phase 1).")
        elif missing:
            if not (legacy_name and vm.get("name") == legacy_name and tags == {"template"}):
                other_run = (any(t.startswith("run-") for t in missing)
                             and any(t.startswith("comp-") for t in tags))
                raise RuntimeError(
                    f"refusing to destroy vmid {vmid} ({vm.get('name')}): its tags '{raw}' are "
                    f"missing {sorted(missing)} — outside this deploy's ownership set"
                    + (" (tagged by a PRE-RUN-ID deploy of this competition: tear it down "
                       "with destroy-competition.py --legacy-tags --yes first)"
                       if other_run else ""))
            print(f"    vmid {vmid} legacy golden '{legacy_name}' — adopting exact reserved slot")
    lock = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"].get("lock")
    if lock:
        unlock_vm(node, vmid, lock)
    if vm.get("status") == "running":
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
        wait_for_proxmox_task(node, upid)
    upid = proxmox_api("DELETE", f"/nodes/{node}/qemu/{vmid}",
                       params={"destroy-unreferenced-disks": 1, "purge": 1})["data"]
    wait_for_proxmox_task(node, upid)


def parse_vm_tags(raw):
    """VM tag string -> set; PVE joins tags with ';' in some views and ',' in others."""
    return {t.strip() for t in str(raw or "").replace(";", ",").split(",") if t.strip()}


def retag_ownership(node, vmid, ownership):
    """Re-stamp an ADOPTED VM's tags to the current run's full ownership set.

    Templates kept across runs (M4 hash reuse) and ranges deployed before run ids
    existed carry the old tag set; without this, the next --full teardown's strict
    guard would skip them as foreign and the next preflight would refuse them as
    clashes. Only re-tags VMs already carrying this competition's comp tag — a
    foreign VM is left alone (loudly)."""
    cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    tags = parse_vm_tags(cfg.get("tags"))
    if ownership <= tags:
        return
    comp_tag = next((t for t in ownership if t.startswith("comp-")), None)
    if comp_tag and comp_tag not in tags:
        print(f"    WARNING: not re-tagging vmid {vmid}: tags '{cfg.get('tags')}' carry no "
              f"'{comp_tag}' — not provably this competition's")
        return
    proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config",
                data={"tags": ";".join(sorted(ownership))})
    print(f"    vmid {vmid}: ownership tags updated to {sorted(ownership)}")



def enumerate_targets(teams, boxes, placement=None, default_node=None):
    """One target per (team, box); build from full lists then filter — vmid is positional.

    With a multi-node placement, each target carries `node` + `slot` from its team's
    placement; without one, `default_node` (the env node) is stamped so downstream
    node-scoped calls have one uniform field."""
    team_node = placement.get("team_nodes") if placement else None
    team_slot = placement.get("team_slots") if placement else None

    def _pve_node(name):
        # placement keys (team_nodes, node record names) are nodes.json NAMES; every
        # /nodes/<name>/... API path needs the PVE hostname inside that record.
        rec = (placement or {}).get("nodes", {}).get(name)
        return rec["node"] if rec else name

    return [
        {
            "team_key": team_key,
            "identifier": str(team["identifier"]),
            "box": box,
            "box_name": box["name"],
            "box_idx": box_idx,
            "ip": f"192.168.{team['identifier']}.{box['last_octet']}",
            "vmid": vm_id_for(team["identifier"], box_idx),
            "vm_name": (f"{team_key}-{box['name']}" if team_key == "team1"
                        else f"{team['identifier']}-{box['name']}"),
            "machine": f"{box['name']}-team{team['identifier']}",
            "node": _pve_node((team_node or {}).get(team_key, default_node)),
            "slot": (team_slot or {}).get(team_key, 0 if placement else None),
        }
        for team_key, team in teams.items()
        for box_idx, box in enumerate(boxes)
    ]


def describe_target(t):
    return f"{t['team_key']}/{t['box_name']}  vmid {t['vmid']}  {t['ip']}"




def list_snapshots(node, vmid):
    """Snapshot names on this VM, excluding Proxmox's synthetic `current`; empty set when unanswerable."""
    try:
        data = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/snapshot")["data"]
    except Exception:
        return set()
    return {s["name"] for s in data if s.get("name") != "current"}


def delete_snapshot(node, vmid, name, timeout=600):
    upid = proxmox_api("DELETE", f"/nodes/{node}/qemu/{vmid}/snapshot/{name}")["data"]
    wait_for_proxmox_task(node, upid, timeout=timeout)


def take_snapshot(node, vmid, name, description="", timeout=900):
    """Take a disk-only snapshot, replacing any existing one; never raises."""
    try:
        if name in list_snapshots(node, vmid):
            delete_snapshot(node, vmid, name)
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/snapshot", data={
            "snapname": name,
            "vmstate": 0,
            "description": description or f"tezcatlipoca {name}",
        })["data"]
        wait_for_proxmox_task(node, upid, timeout=timeout)
        return True
    except Exception as e:
        print(f"    WARNING: snapshot '{name}' failed for vmid {vmid}: {e}")
        # Never raises by design, but this is NOT a cosmetic warning: `tz-base`/`tz-ready`
        # are the rollback points, so a range whose snapshot failed cannot be rolled back
        # (`redeploy --mode rollback-base`) and a failed golden snapshot removes the
        # pre-plant guard. Live-found 2026-10-02: hdd filled during a Windows-heavy run and
        # both snapshots failed with `zfs error: ... out of space`, silently, mid-deploy.
        record_degradation(f"snapshot '{name}' failed",
                           f"vmid {vmid}: {str(e)[:200]}")
        return False


def rollback_snapshot(node, vmid, name, timeout=900, restart=True):
    """Rollback to snapshot (requires stop/start for no-RAM snapshot). Raises on failure."""

    if vm_status(node, vmid) == "running":
        stop_vm(node, vmid)
    upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/snapshot/{name}/rollback")["data"]
    wait_for_proxmox_task(node, upid, timeout=timeout)
    if restart:
        start_vm(node, vmid)


def snapshot_support_hint(node, vmid):
    """One-line explanation for why snapshots may be unavailable, for error messages."""
    return (
        f"vmid {vmid} on node {node} has no tezcatlipoca snapshots. Either this range was "
        f"deployed before snapshotting was added, or TF_VAR_datastore doesn't support "
        f"snapshots (thick LVM can't; qcow2 on file storage, ZFS, LVM-thin and Ceph can). "
        f"Use --mode reconfigure (re-runs configuration on the live box) or --mode rebuild "
        f"(recreates the VM from its template)."
    )
