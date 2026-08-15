"""Proxmox API layer, VM identity math, and the (team, box) target abstraction.

This started as private helpers inside create-competition.py. It moved out here so
redeploy-competition.py can drive the same infrastructure without importing the 2000-line
deploy driver just to reach a REST call — and so destroy-competition.py stops carrying its own
divergent copy of proxmox_api()/wait_for_proxmox_task().

Nothing here prompts, writes to competitions/, or knows about deploy phases. It reads
TF_VAR_proxmox_* out of the environment (the importing script is responsible for load_dotenv).
"""

import os
import time

import requests

# Proxmox's API token auth talks straight to the API over the same self-signed cert main.tf's
# provider block sets insecure=true for — same tradeoff, just from Python instead. Each entry
# point that imports this module also calls urllib3.disable_warnings().


### Proxmox API

def proxmox_api(method, path, **kwargs):
    endpoint = os.environ["TF_VAR_proxmox_endpoint"].rstrip("/")
    url = f"{endpoint}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={os.environ['TF_VAR_proxmox_api_token']}"}
    # This host has shown occasional transient network blips under load (pveproxy dropping a
    # connection mid-request, a `RemoteDisconnected` with no HTTP response at all) — seen live
    # killing an otherwise-healthy long-running deploy at a routine task-status poll. That's not
    # an API error (no status code to even check) so it can't be told apart from a real outage
    # by response content; a few short retries absorb the blip without masking a genuinely dead
    # host; a call that's ACTUALLY down still exhausts these fast and raises as before.
    last_exc = None
    for attempt in range(4):
        try:
            r = requests.request(method, url, headers=headers, verify=False, timeout=60, **kwargs)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_exc = e
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
    raise last_exc


def wait_for_proxmox_task(node, upid, timeout=1800):
    # 600s used to be the default; seen live on this host twice now — a VM clone that ran past
    # 90 minutes under concurrent disk contention (host-level, not this tool's doing — see the
    # 2026-08 incident notes) and, separately, an ordinary VM delete that took a bit over 600s
    # with nothing wrong (the task itself reported exitstatus OK once checked directly). A
    # genuinely wedged task still needs a human/manual unlock regardless of the number here; this
    # bump just stops an everyday slow patch on this host from failing a run outright.
    deadline = time.time() + timeout
    while True:
        if time.time() > deadline:
            raise RuntimeError(f"Proxmox task {upid} timed out after {timeout}s")
        data = proxmox_api("GET", f"/nodes/{node}/tasks/{upid}/status")["data"]
        if data["status"] == "stopped":
            if data.get("exitstatus") != "OK":
                raise RuntimeError(f"Proxmox task {upid} failed: {data.get('exitstatus')}")
            return
        time.sleep(3)


def diagnose_unreachable_box(node, vmid):
    """Best-effort diagnosis for a box that never became SSH-reachable, using the QEMU guest
    agent — it works over virtio-serial regardless of network state, so it can tell us WHY a box
    has no route (broken cloud-init, no IP at all, ...) instead of just that it's unreachable.
    A blocked/failed cloud-init that never brings up the NIC looks identical to a slow-booting
    box from the outside (both are "no route to host" for the full retry budget); this is the
    difference between finding that out in seconds versus after 8-attempt retry loops in several
    downstream steps all fail the same way. Never raises — purely diagnostic, and the guest
    agent itself may be unreachable (e.g. not yet started, or genuinely no network at all).
    """
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
        pass  # keep the default "(cloud-init status unavailable)" — non-fatal either way

    return f"      guest agent (vmid {vmid}): {iface_summary}; cloud-init {cloud_init_summary}"


def guest_agent_exec_root(node, vmid, script, timeout=60):
    """Run a bash script as root via the QEMU guest agent (virtio-serial, not the network).

    The agent daemon is root's own process, so this needs no sudo at all — unlike ssh_via_gateway
    + a script full of `sudo` commands, it isn't affected by a box's *own* sudo trust being
    broken (e.g. the writable-sudoers misconfig makes /etc/sudoers.d insecure, so modern sudo
    refuses to honor sudo-nopasswd's NOPASSWD rule and demands a real password that doesn't
    exist for the key-only cloud-init user — see fix_services_on_boxes). Deliberately does NOT
    "fix" that misconfig; it just gives our own provisioning a channel that doesn't depend on it,
    so the vuln stays intact for whoever's meant to find it.

    Returns (exit_code, stdout, stderr); raises on a guest-agent-level failure (agent
    unreachable, exec never returned) since callers should treat that differently from the
    *command* failing.
    """
    pid = proxmox_api(
        "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
        data={"command": ["bash", "-c", script]},
    )["data"]["pid"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
            params={"pid": pid},
        )["data"]
        if s.get("exited"):
            return s.get("exitcode", -1), s.get("out-data", ""), s.get("err-data", "")
        time.sleep(1)
    raise RuntimeError(f"guest-agent exec on vmid {vmid} didn't finish within {timeout}s")


def guest_agent_exec_windows(node, vmid, ps_script, timeout=120):
    """Run a PowerShell script as SYSTEM via the QEMU guest agent — the Windows sibling of
    guest_agent_exec_root(). Same virtio-serial channel, same no-sudo-equivalent-needed
    property (the agent daemon runs as LocalSystem), which is exactly what's needed here: a
    freshly cloned Windows box has no cloud-init/cloudbase-init equivalent to set its IP,
    credentials or SSH access, so this is the ONLY channel available until that bootstrap has
    run (see bootstrap_windows_box() in create-competition.py).

    -EncodedCommand avoids the usual quoting minefield of smuggling a multi-line script through
    the agent's ["powershell.exe", ..., "-Command", script] argv (embedded quotes/newlines get
    mangled across the JSON->exec->cmd.exe hops); base64-UTF16LE is what powershell.exe itself
    expects for this flag.

    Returns (exit_code, stdout, stderr); raises on a guest-agent-level failure (agent
    unreachable, exec never returned), same contract as guest_agent_exec_root().
    """
    import base64
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    pid = proxmox_api(
        "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
        data={"command": [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
        ]},
    )["data"]["pid"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
            params={"pid": pid},
        )["data"]
        if s.get("exited"):
            return s.get("exitcode", -1), s.get("out-data", ""), s.get("err-data", "")
        time.sleep(1)
    raise RuntimeError(f"guest-agent exec on vmid {vmid} didn't finish within {timeout}s")


def wait_for_guest_agent(node, vmid, timeout=300):
    """Block until the QEMU guest agent responds to a ping — works over virtio-serial with no
    dependency on the guest having any network at all, which is exactly the gap a Windows box
    needs covered: it has no cloud-init/cloudbase-init to signal "I'm up", and until
    bootstrap_windows_box() has run it may not even have an IP address yet. Used both right
    after a fresh clone and after a reboot triggered mid-plan (ADDS/Domain Join).

    Never raises — a timeout returns False and callers decide how to react (this mirrors
    wait_for_ssh()'s posture elsewhere in the toolchain).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/agent/ping", data={})
            return True
        except Exception:
            pass
        time.sleep(5)
    return False


### VM identity

# VM IDs are 200 + identifier*10 + box_index (mirrored in main.tf's team_box.vm_id), which
# leaves each team a stride of exactly 10. An 11th box would land on the next team's first box
# and silently clobber it, so the scheme caps the box count rather than the picker.
MAX_BOXES_PER_TEAM = 10

# Team identifiers are `100 + i` and become the subnet's third octet (192.168.<identifier>.x),
# which must stay a valid, non-zero octet (1-254) — i.e. i <= 154. Past that, identifiers like
# 256 would produce an invalid IP and silently break the whole range.
MAX_TEAMS = 154

# The scoring engine's vmid is fixed in main.tf, not derived from the formula above.
SCORING_ENGINE_VMID = 1000


def vm_id_for(identifier, box_index):
    return 200 + int(identifier) * 10 + box_index


### VM lifecycle

def vm_status(node, vmid):
    """Current status string ('running'/'stopped'/...) or None if the VM doesn't exist."""
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    vm = next((v for v in vms if v["vmid"] == vmid), None)
    return vm.get("status") if vm else None


def stop_vm(node, vmid):
    # Graceful first so the box's filesystem is consistent for the clone, but never
    # indefinitely: a shutdown is an ACPI power-button event, and a guest without acpid (box
    # templates are only required to have working cloud-init) just ignores it. That used to
    # stall here for the full task timeout and then abort the deploy — after nakon had already
    # run — so fall back to pulling the plug instead.
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


def destroy_vm_if_exists(node, vmid):
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    vm = next((v for v in vms if v["vmid"] == vmid), None)
    if vm is None:
        return
    if vm.get("status") == "running":
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
        wait_for_proxmox_task(node, upid)
    upid = proxmox_api("DELETE", f"/nodes/{node}/qemu/{vmid}")["data"]
    wait_for_proxmox_task(node, upid)


### Targets — the (team, box) pair every per-box operation actually works on

def enumerate_targets(teams, boxes):
    """One target dict per (team, box), carrying the TRUE index of the box in `boxes`.

    Every per-box operation needs four different names for the same machine — an IP, a vmid, a
    Proxmox VM name, and a nakon machine name — and three of them are derived, not stored. The
    derivation that matters is the vmid: vm_id_for() takes the box's 0-based POSITION in the
    competition's full box list, which is only the same thing as `enumerate(boxes)` when `boxes`
    is the complete list.

    That is exactly why this function exists. The callers used to iterate
    `for team in teams.values(): for box_idx, box in enumerate(boxes)` inline, which silently
    computes wrong vmids the moment anyone passes a filtered subset of boxes — the same class of
    bug that once made deploy()'s cleanup phase miss every team box (it passed last_octet where
    a box index was wanted). Build the full target list ONCE from the full inputs, then filter
    the targets; never filter `boxes` and re-enumerate.
    """
    return [
        {
            "team_key": team_key,
            "identifier": str(team["identifier"]),
            "box": box,
            "box_name": box["name"],
            "box_idx": box_idx,
            "ip": f"192.168.{team['identifier']}.{box['last_octet']}",
            "vmid": vm_id_for(team["identifier"], box_idx),
            # Proxmox names team1's boxes "team1-<box>" (main.tf's local.team_vms) but the
            # API-created clones "<identifier>-<box>" (clone_team_boxes). Record what this
            # target's VM is actually called so nothing has to re-derive it.
            "vm_name": (f"{team_key}-{box['name']}" if team_key == "team1"
                        else f"{team['identifier']}-{box['name']}"),
            # nakon's machine list uses the opposite order — see generate_nakon_config().
            "machine": f"{box['name']}-team{team['identifier']}",
        }
        for team_key, team in teams.items()
        for box_idx, box in enumerate(boxes)
    ]


def describe_target(t):
    return f"{t['team_key']}/{t['box_name']}  vmid {t['vmid']}  {t['ip']}"


### Snapshots

# Taken before nakon plants anything: the box boots, has working DNS and a usable `ubuntu`
# login, and nothing else. Rolling back here and re-running nakon reproduces the deploy.
#
# One asymmetry worth knowing: team1's tz-base really is a bare box, but team2+ boxes are
# full-cloned from team1 AFTER phase 5's nakon run, so their tz-base already carries team1's
# planted configurations. It is still the correct "before we configured THIS box" point —
# tz-base + `nakon deploy --only <machine>` + fix_services_on_boxes() is precisely what phase 6
# does to a freshly cloned box — the two just don't hold identical bits.
SNAP_BASE = "tz-base"

# Taken at the end of phase 6, after the final nakon pass and service hardening: the exact box
# the competition starts on. This is what a mid-competition rollback restores.
SNAP_READY = "tz-ready"


def list_snapshots(node, vmid):
    """Snapshot names on this VM, excluding Proxmox's synthetic `current` entry.

    Returns an empty set if the VM is gone or the API refuses — callers treat "no snapshot" and
    "couldn't ask" the same way (fall back to a mode that doesn't need one).
    """
    try:
        data = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/snapshot")["data"]
    except Exception:
        return set()
    return {s["name"] for s in data if s.get("name") != "current"}


def delete_snapshot(node, vmid, name, timeout=600):
    upid = proxmox_api("DELETE", f"/nodes/{node}/qemu/{vmid}/snapshot/{name}")["data"]
    wait_for_proxmox_task(node, upid, timeout=timeout)


def take_snapshot(node, vmid, name, description="", timeout=900):
    """Take a disk-only (no-RAM) snapshot. Returns True on success, False on any failure.

    `vmstate: 0` is what keeps this cheap — no RAM dump, so it's a storage-level operation that
    finishes in seconds instead of writing gigabytes. The boxes run qemu-guest-agent (see
    guest_agent_exec_root), so Proxmox issues a guest fsfreeze around it and the image is
    filesystem-consistent rather than merely crash-consistent.

    Deliberately never raises. Snapshots are a recovery convenience layered onto the deploy, not
    a prerequisite for it: a datastore that can't snapshot (thick LVM, as opposed to qcow2 on
    file storage / ZFS / LVM-thin / Ceph) must not take down an otherwise-good competition
    build. The failure is printed so the operator knows redeploy-competition.py's rollback modes
    won't be available for this range.

    An existing snapshot of the same name is deleted first, so re-running a deploy phase
    (--from-phase) re-snapshots the state it just rebuilt instead of failing on a name clash.
    """
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
        return False


def rollback_snapshot(node, vmid, name, timeout=900, restart=True):
    """Roll a VM back to a snapshot: stop, rollback, start.

    Proxmox refuses to roll back a running VM, and a no-RAM snapshot has no saved machine state
    to resume into anyway — so the stop/start round trip is mandatory, not defensive. Raises if
    the rollback itself fails; the caller decides whether one bad box aborts the batch.
    """
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
