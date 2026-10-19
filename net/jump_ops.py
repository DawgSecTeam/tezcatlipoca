"""Per-satellite jump/router VM: build, configure, verify."""

import secrets
import subprocess
import time

from common.constants import DEFAULT_ENGINE_MGMT_GW, ownership_tags
from net.jump_rules import (APT_CACHER_PORT, JUMP_TAGS_EXTRA, jump_rules, jump_sysctl_script,  # noqa: F401
                        red_segment_from_env)
from infra.guest_exec import wait_for_guest_agent
from infra.client import cluster_vms_for, proxmox_api, wait_for_proxmox_task
from infra.lifecycle import start_vm, vm_status
from infra.ownership import clone_marker, destroy_vm_if_exists, gc_orphan_volumes
from common.ssh_ops import DEFAULT_KNOWN_HOSTS, forget_engine_host_key
from common.utils import PRINT_LOCK, run_concurrent
from multinode.placement_record import resolved_mgmt_bridge
from estate import config



def find_jump_template(node, hint=""):
    hint = hint or "alpine"
    matches = {}
    for vm in cluster_vms_for(node):
        if vm.get("node") != node or vm.get("template") != 1 or vm.get("status") != "stopped":
            continue
        tags = {t.strip() for t in str(vm.get("tags") or "").replace(";", ",").split(",") if t.strip()}
        if "template" in tags and hint in str(vm.get("name") or "").lower():
            matches[vm["name"]] = vm["vmid"]
    if not matches:
        raise SystemExit(
            f"  ERROR: no stopped template matching '{hint}' on node '{node}' — the jump "
            f"VM clone needs one. Available templates there: "
            f"{sorted(n for n in _node_template_names(node)) or '(none)'}. Build one "
            "(docs/usage-people.md) or set jump_template in nodes.json.")
    return matches


def _node_template_names(node):
    out = []
    for vm in cluster_vms_for(node):
        if vm.get("node") != node or vm.get("template") != 1:
            continue
        tags = {t.strip() for t in str(vm.get("tags") or "").replace(";", ",").split(",") if t.strip()}
        if "template" in tags:
            out.append(vm.get("name"))
    return out


def _jump_ssh(ip, user, key_path, cmd, timeout=60):
    # Deploy host and jump share the mgmt LAN, so no engine hop is needed.
    return subprocess.run(
        ["ssh", "-i", key_path,
         "-o", "StrictHostKeyChecking=accept-new",
         "-o", f"UserKnownHostsFile={DEFAULT_KNOWN_HOSTS}",
         "-o", "ConnectTimeout=10",
         f"{user}@{ip}", cmd],
        capture_output=True, text=True, timeout=timeout)


def wait_jump_ready(node, vmid, ip, user, key_path, expected_key, timeout=900):
    # A fresh clone carries the template's build key and is shadow-locked, so key-matching
    # alone passes on stale state. Cold clones ~10 min.
    from infra.guest_exec import guest_agent_exec_root
    key_body = expected_key.split()[1] if len(expected_key.split()) > 1 else expected_key
    probe = (f"cloud-init status 2>/dev/null | grep -q done && "
             f"grep -qF '{key_body}' /home/sysadmin/.ssh/authorized_keys && "
             "! grep -q '^sysadmin:!' /etc/shadow && echo READY")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, out, _ = guest_agent_exec_root(node, vmid, probe, timeout=30, shell="sh")
            if rc == 0 and "READY" in out:
                break
        except Exception:
            pass
        time.sleep(10)
    else:
        return False
    ssh_deadline = time.time() + 120
    while time.time() < ssh_deadline:
        try:
            r = _jump_ssh(ip, user, key_path, "echo ok", timeout=20)
            if r.returncode == 0 and "ok" in r.stdout:
                return True
        except (subprocess.SubprocessError, OSError):
            pass
        time.sleep(5)
    return False


def configure_jump(ip, user, key_path, team_identifiers, engine_mgmt_ip,
                   red_segment=""):
    rules = jump_rules(team_identifiers, engine_mgmt_ip, red_segment=red_segment)
    quoted = subprocess.list2cmdline([rules]).replace("'", "'\\''")
    probe = _jump_ssh(ip, user, key_path,
                      "command -v apk >/dev/null 2>&1 && echo alpine || echo debian",
                      timeout=30)
    alpine = "alpine" in (probe.stdout or "")
    if alpine:
        # Alpine's iptables OpenRC ignores rules.v4: apply live, then rc-service save persists.
        cmds = [
            "command -v iptables-restore >/dev/null || sudo apk add --no-cache iptables",
            f"printf '%s' {quoted} | sudo tee /etc/iptables/rules.v4 > /dev/null",
            "sudo iptables-restore < /etc/iptables/rules.v4",
            "sudo rc-service iptables save",
            "sudo rc-update add iptables default || true",
            jump_sysctl_script(),
        ]
    else:
        # Debian: SSH non-login PATH lacks /usr/sbin; static-ip cloud-init may leave no resolver.
        path = "export PATH=$PATH:/usr/sbin:/sbin; "
        resolver = ("printf 'nameserver 10.0.0.1\\nnameserver 1.1.1.1\\n' | "
                    "sudo tee /etc/resolv.conf > /dev/null")
        cmds = [
            resolver,
            path + "command -v iptables-restore >/dev/null || "
            "sudo DEBIAN_FRONTEND=noninteractive sudo apt-get install -y -qq iptables",
            path + "sudo mkdir -p /etc/iptables && "
            f"printf '%s' {quoted} | sudo tee /etc/iptables/rules.v4 > /dev/null",
            path + "sudo iptables-restore < /etc/iptables/rules.v4",
            path + "sudo DEBIAN_FRONTEND=noninteractive sudo apt-get install -y -qq "
            "iptables-persistent >/dev/null 2>&1 || true",
            path + "sudo systemctl enable --now netfilter-persistent >/dev/null 2>&1 || true; "
            "sudo iptables-restore < /etc/iptables/rules.v4",
            path + jump_sysctl_script(),
        ]
    for cmd in cmds:
        r = _jump_ssh(ip, user, key_path, cmd, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(
                f"jump configuration step failed on {ip}: {cmd.split(';')[0][:60]}... "
                f"rc={r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")


def _log(*args, **kwargs):
    with PRINT_LOCK:
        print(*args, **kwargs)


def build_jump_vms(placement, engine_vmid, ctx, comp_name, engine_mgmt_ip,
                   engine_mgmt_gw=DEFAULT_ENGINE_MGMT_GW, engine_node_name=None,
                   run_id=None, red_segment=""):
    # Bounded at 4: each worker is a full clone onto a datastore.
    from multinode.placement_record import record_of
    sats = list(placement["satellites"])

    def _one(sat):
        rec = record_of(placement, sat["name"])
        vmid = sat["jump_vmid"]
        with PRINT_LOCK:
            print(f"  Jump VM for satellite '{sat['name']}' (slot {sat['slot']}, "
                  f"vmid {vmid}, mgmt {sat['jump_mgmt_ip']})...")
        team_ids = sorted({placement["team_identifiers"][k] for k in sat["teams"]}, key=int)
        _build_one(rec, sat, vmid, team_ids, ctx, comp_name, engine_mgmt_ip, engine_mgmt_gw,
                   run_id=run_id, red_segment=red_segment,
                   mgmt_bridge=resolved_mgmt_bridge(placement))

    results = run_concurrent(sats, _one, max_workers=4)
    # run_concurrent captures exceptions per slot; one dead satellite aborts the deploy.
    for sat, r in zip(sats, results):
        if isinstance(r, Exception):
            raise r
    return {sat["name"]: sat["jump_vmid"] for sat in sats}


def _build_one(rec, sat, vmid, team_ids, ctx, comp_name, engine_mgmt_ip, engine_mgmt_gw,
               run_id=None, red_segment="", mgmt_bridge="vmbr0"):
    node = rec.node
    ownership = ownership_tags(comp_name, run_id, JUMP_TAGS_EXTRA)
    vm_name = f"jump-{comp_name}-{sat['slot']}"

    reuse = False
    try:
        cfg0 = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
        reuse = (clone_marker(comp_name) in str(cfg0.get("description") or "")
                 and vm_status(node, vmid) == "running")
    except Exception:
        reuse = False

    if reuse:
        _log("    jump VM already running from a previous attempt — reconfiguring in place")
        # This run's teardown guards must recognize a jump adopted from an earlier attempt.
        from infra.ownership import retag_ownership
        try:
            retag_ownership(node, vmid, ownership)
        except Exception as e:
            _log(f"    WARNING: could not re-tag jump vmid {vmid}: {str(e)[:120]}")
    else:
        templates = find_jump_template(node, rec.jump_template)
        if len(templates) > 1:
            _log(f"    WARNING: multiple jump-template candidates on {node}: "
                  f"{sorted(templates)} — using '{sorted(templates)[0]}'")
        src = templates[sorted(templates)[0]]

        destroy_vm_if_exists(node, vmid, expect_tags=ownership)
        gc_orphan_volumes(node, vmid)
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{src}/clone", data={
            "newid": vmid, "name": vm_name, "full": 1,
            "description": clone_marker(comp_name)})["data"]
        wait_for_proxmox_task(node, upid)

        nics = {"net0": f"virtio,bridge={rec.mgmt_bridge or mgmt_bridge}"}
        for i, t in enumerate(team_ids):
            nics[f"net{i + 1}"] = f"virtio,bridge=vmbr{t}"
        cfg = {
            **nics,
            "tags": ";".join(sorted(ownership)),
            "ciuser": ctx.get("vm_username", "sysadmin"),
            "sshkeys": ctx["ssh_public_key_quoted"],
            "ipconfig0": f"ip={sat['jump_mgmt_ip']}/24,gw={engine_mgmt_gw}",
            # Alpine adduser shadow-locks; cloud-init unlocks only when a password is set.
            "cipassword": secrets.token_urlsafe(12),
        }
        for i, t in enumerate(team_ids):
            cfg[f"ipconfig{i + 1}"] = f"ip=192.168.{t}.1/24"
        # Template cicustom user-data overrides generated ciuser/sshkeys; strip before first boot.
        try:
            proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config", data={"delete": "cicustom"})
        except Exception as e:
            _log(f"    cicustom strip on {vmid} failed (template may not carry one): "
                  f"{str(e)[:100]}")
        proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config", data=cfg)

        # First start can outlive start_vm's budget on a busy host — tolerate a timed-out-but-running start.
        try:
            start_vm(node, vmid, timeout=300)
        except RuntimeError:
            if vm_status(node, vmid) != "running":
                raise
            _log("    start task outlived its wait — VM is running anyway")
        if not wait_for_guest_agent(node, vmid, timeout=180):
            _log("    WARNING: jump agent not answering (cloud-init may still be running)")
    user = ctx.get("vm_username", "sysadmin")
    # Clones regenerate host keys; the prior jump's pin at this IP makes accept-new refuse.
    forget_engine_host_key(sat["jump_mgmt_ip"])
    if not wait_jump_ready(node, vmid, sat["jump_mgmt_ip"], user, ctx["ssh_key_path"],
                           config.active().ssh.public_key):
        raise RuntimeError(f"jump {vm_name} on '{node}' never came up on SSH at "
                           f"{sat['jump_mgmt_ip']} — check its console")
    configure_jump(sat["jump_mgmt_ip"], user, ctx["ssh_key_path"], team_ids, engine_mgmt_ip,
                   red_segment=red_segment)
    _log(f"    jump-{comp_name}-{sat['slot']} configured and forwarding "
          f"(teams {', '.join(team_ids)})")


def destroy_jump_vms(placement, comp_tags):
    from multinode.placement_record import record_of
    for sat in placement.get("satellites", []):
        rec = record_of(placement, sat["name"])
        try:
            destroy_vm_if_exists(rec.node, sat["jump_vmid"], expect_tags=comp_tags | {JUMP_TAGS_EXTRA})
        except RuntimeError as e:
            print(f"    WARNING: jump vmid {sat['jump_vmid']} on '{rec.node}': {e}")

__all__ = [
    "APT_CACHER_PORT",
    "JUMP_TAGS_EXTRA",
    "build_jump_vms",
    "configure_jump",
    "destroy_jump_vms",
    "find_jump_template",
    "jump_rules",
    "jump_sysctl_script",
    "red_segment_from_env",
    "wait_jump_ready",
]
