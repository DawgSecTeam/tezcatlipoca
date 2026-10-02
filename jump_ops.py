"""Per-satellite jump/router VM: build, configure, verify.

The jump impersonates the scoring engine at L3 on the satellite's team bridges: it
holds 192.168.<id>.1 for each locally-placed team (the address every box knows as
its gateway), DNATs the gateway IP's apt-cacher port to the engine, and SNATs
engine->box traffic so the boxes' gateway-IP-only SSH trust still sees their
gateway. Forwarding is default-DROP with explicit accepts — team-to-team isolation
is structural, matching the engine's own FORWARD rule. From every box's perspective
nothing changes (docs/multi-node.md).

Built via the direct API path (golden_ops precedent — outside terraform's lifecycle,
destroyed by computed vmid in phase 1 / destroy-competition), one per satellite,
cloned from the node's alpine base template. Addressing rides PVE cloud-init
(ipconfig0 mgmt + ipconfig1..n team gateways); forwarding/NAT rules are pushed over
SSH after first boot and made persistent with Alpine's iptables init service."""

import os
import secrets
import subprocess
import time

from constants import DEFAULT_ENGINE_MGMT_GW, ownership_tags
from range_ops import (
    cluster_vms_for,
    vm_status,
    clone_marker,
    destroy_vm_if_exists,
    gc_orphan_volumes,
    proxmox_api,
    start_vm,
    wait_for_guest_agent,
    wait_for_proxmox_task,
)
from ssh_ops import DEFAULT_KNOWN_HOSTS, forget_engine_host_key
from utils import PRINT_LOCK, run_concurrent

JUMP_TAGS_EXTRA = "jump"
APT_CACHER_PORT = 3142


def jump_rules(team_identifiers, engine_mgmt_ip, mgmt_cidr="10.0.0.0/24"):
    """iptables-restore content for the jump. Pure — golden-filed in tests.

    FORWARD defaults to DROP; accepts are exactly: established/related, engine ->
    team subnets (scoring/nakon/verify), team -> engine (apt/DNAT callbacks), and
    team -> anything outside 192.168.0.0/16 (internet + mgmt LAN — the same egress
    the engine's MASQUERADE grants local teams). Team -> team and mgmt -> team fall
    off the default DROP: structural isolation."""
    teams = [str(i) for i in team_identifiers]
    engine = f"{engine_mgmt_ip}/32"
    lines = ["*filter",
             ":INPUT ACCEPT",
             ":FORWARD DROP",
             ":OUTPUT ACCEPT",
             "-A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"]
    for t in teams:
        lines.append(f"-A FORWARD -s {engine} -d 192.168.{t}.0/24 -j ACCEPT")
    for t in teams:
        lines.append(f"-A FORWARD -s 192.168.{t}.0/24 -d {engine} -j ACCEPT")
    for t in teams:
        lines.append(f"-A FORWARD -s 192.168.{t}.0/24 ! -d 192.168.0.0/16 -j ACCEPT")
    lines += ["COMMIT", "*nat"]
    for t in teams:
        lines.append(f"-A PREROUTING -d 192.168.{t}.1/32 -p tcp --dport {APT_CACHER_PORT} "
                     f"-j DNAT --to-destination {engine_mgmt_ip}:{APT_CACHER_PORT}")
    for t in teams:
        # Engine -> box traffic sources from the gateway IP: boxes trust SSH only
        # from 192.168.<id>.1 (gateway-IP auth preserved across the routed hop).
        lines.append(f"-A POSTROUTING -s {engine} -d 192.168.{t}.0/24 "
                     f"-j SNAT --to-source 192.168.{t}.1")
    for t in teams:
        # Engine-bound traffic is left UNmangled via a terminal ACCEPT (first match
        # wins in nat POSTROUTING) — the engine sees real box sources, same as with
        # local teams today. iptables-restore rejects two -d in one rule, so the
        # exclusion can't ride the MASQUERADE line itself.
        lines.append(f"-A POSTROUTING -s 192.168.{t}.0/24 -d {engine} -j ACCEPT")
        lines.append(f"-A POSTROUTING -s 192.168.{t}.0/24 ! -d 192.168.0.0/16 -j MASQUERADE")
    lines.append("COMMIT")
    return "\n".join(lines) + "\n"


def jump_sysctl_script():
    return ("printf 'net.ipv4.ip_forward=1\\nnet.ipv4.conf.all.rp_filter=2\\n"
            "net.ipv4.conf.default.rp_filter=2\\n' | sudo tee /etc/sysctl.d/99-jump.conf; "
            "sudo sysctl -p /etc/sysctl.d/99-jump.conf")


def find_jump_template(node, hint=""):
    """The satellite's alpine base template (jump clone source). Name-substring match,
    stopped, tagged `template`, ON this node — mirrors _template_vmid_map's rules."""
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
    """Direct SSH to the jump's mgmt IP (deploy host and jump share the mgmt LAN;
    no engine hop needed)."""
    return subprocess.run(
        ["ssh", "-i", key_path,
         "-o", "StrictHostKeyChecking=accept-new",
         "-o", f"UserKnownHostsFile={DEFAULT_KNOWN_HOSTS}",
         "-o", "ConnectTimeout=10",
         f"{user}@{ip}", cmd],
        capture_output=True, text=True, timeout=timeout)


def wait_jump_ready(node, vmid, ip, user, key_path, expected_key, timeout=900):
    """Wait for the jump to be SSH-able, watching the PREREQUISITES over the guest
    agent (no SSH needed): cloud-init must have installed THE DEPLOY KEY (a fresh
    clone carries the template's build key in authorized_keys before cloud-init
    runs — "non-empty" would pass on stale state) and unlocked the account (a
    shadow-locked user is refused even with a valid key). Cold clones take ~10 min
    from first agent ping to sshd-ready, so the agent channel is the signal; only
    then poll SSH briefly."""
    from range_ops import guest_agent_exec_root
    key_body = expected_key.split()[1] if len(expected_key.split()) > 1 else expected_key
    # cloud-init-done is the real gate: the template's build key IS the deploy key
    # (same operator key built the template), so key-matching alone passes instantly
    # on stale pre-cloud-init state — while cloud-init is mid-rewrite of exactly the
    # files (keys/shadow) that make SSH work.
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


def configure_jump(ip, user, key_path, team_identifiers, engine_mgmt_ip):
    """Push forwarding + NAT rules and make them persistent (Alpine iptables init)."""
    rules = jump_rules(team_identifiers, engine_mgmt_ip)
    quoted = subprocess.list2cmdline([rules]).replace("'", "'\\''")
    # Alpine's iptables OpenRC service ignores /etc/iptables/rules.v4 (Debian's
    # persistent path): apply the rules LIVE, then `rc-service iptables save`
    # persists them to Alpine's save file, which the boot service loads.
    cmds = [
        "command -v iptables-restore >/dev/null || sudo apk add --no-cache iptables",
        f"printf '%s' {quoted} | sudo tee /etc/iptables/rules.v4 > /dev/null",
        "sudo iptables-restore < /etc/iptables/rules.v4",
        "sudo rc-service iptables save",
        "sudo rc-update add iptables default || true",
        jump_sysctl_script(),
    ]
    for cmd in cmds:
        r = _jump_ssh(ip, user, key_path, cmd, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(
                f"jump configuration step failed on {ip}: {cmd.split(';')[0][:60]}... "
                f"rc={r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    verify_jump(ip, user, key_path)


def verify_jump(ip, user, key_path):
    r = _jump_ssh(ip, user, key_path,
                  "sysctl -n net.ipv4.ip_forward; iptables -S FORWARD | wc -l", timeout=30)
    out = (r.stdout or "").split()
    if r.returncode != 0 or not out or out[0].strip() != "1":
        raise RuntimeError(f"jump {ip} not forwarding after configuration "
                           f"(rc={r.returncode}, out={(r.stdout or '').strip()[:80]})")



def _log(*args, **kwargs):
    """print() under the shared lock: _build_one runs on a thread pool (build_jump_vms),
    so an unwrapped multi-line print from one satellite interleaves into another's."""
    with PRINT_LOCK:
        print(*args, **kwargs)


def build_jump_vms(placement, engine_vmid, ctx, comp_name, engine_mgmt_ip,
                   engine_mgmt_gw=DEFAULT_ENGINE_MGMT_GW, engine_node_name=None,
                   run_id=None):
    """Clone + configure one jump per satellite. Returns {node_name: jump_vmid}.

    Satellites run CONCURRENTLY. Each is a different Proxmox host reached over the
    direct API path (node-scoped calls follow the placement route table) with its SSH
    configuration going over the mgmt LAN, and each build is dominated by a full clone
    plus a first-boot cloud-init wait — measured 17s to 547s, avg ~150s, across
    17 samples in multinode-spread-2026-09-30. Serially that made an 8-satellite
    spread pay ~18 minutes of pure waiting for no reason.

    Bounded at 4 rather than the pool's default 8: unlike the per-box work, each unit
    here is a *full* clone onto a datastore, and a satellite host may be running
    several. 4 keeps clone-write saturation and the Proxmox task load in the range the
    M0.2/M2.1 benchmarks validated, while still collapsing the wait.
    """
    from nodes_ops import record_of
    sats = list(placement["satellites"])

    def _one(sat):
        rec = record_of(placement, sat["name"])
        vmid = sat["jump_vmid"]
        with PRINT_LOCK:
            print(f"  Jump VM for satellite '{sat['name']}' (slot {sat['slot']}, "
                  f"vmid {vmid}, mgmt {sat['jump_mgmt_ip']})...")
        team_ids = sorted({placement["team_identifiers"][k] for k in sat["teams"]}, key=int)
        _build_one(rec, sat, vmid, team_ids, ctx, comp_name, engine_mgmt_ip, engine_mgmt_gw,
                   run_id=run_id)

    results = run_concurrent(sats, _one, max_workers=4)
    # run_concurrent captures exceptions per slot instead of raising, so each caller
    # keeps its own aggregate semantics. Here the semantics are unchanged from the
    # serial loop: one satellite that never comes up aborts the deploy.
    for sat, r in zip(sats, results):
        if isinstance(r, Exception):
            raise r
    return {sat["name"]: sat["jump_vmid"] for sat in sats}


def _build_one(rec, sat, vmid, team_ids, ctx, comp_name, engine_mgmt_ip, engine_mgmt_gw,
               run_id=None):
    node = rec.node
    ownership = ownership_tags(comp_name, run_id, JUMP_TAGS_EXTRA)
    vm_name = f"jump-{comp_name}-{sat['slot']}"

    # Resume short-circuit: a running, ours jump from a failed attempt is
    # reconfigured in place instead of re-cloned (another ~10 min cloud-init wait).
    reuse = False
    try:
        cfg0 = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
        reuse = (clone_marker(comp_name) in str(cfg0.get("description") or "")
                 and vm_status(node, vmid) == "running")
    except Exception:
        reuse = False

    if reuse:
        _log("    jump VM already running from a previous attempt — reconfiguring in place")
        # Re-stamp ownership so a jump adopted from an older attempt (e.g. a state
        # that predates run ids) is recognized by this run's teardown guards.
        from range_ops import retag_ownership
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

        nics = {"net0": "virtio,bridge=vmbr0"}
        for i, t in enumerate(team_ids):
            nics[f"net{i + 1}"] = f"virtio,bridge=vmbr{t}"
        cfg = {
            **nics,
            "tags": ";".join(sorted(ownership)),
            "ciuser": ctx.get("vm_username", "sysadmin"),
            "sshkeys": ctx["ssh_public_key_quoted"],
            "ipconfig0": f"ip={sat['jump_mgmt_ip']}/24,gw={engine_mgmt_gw}",
            # Alpine's adduser leaves a fresh account shadow-locked ('!'), and sshd
            # refuses pubkey auth to a locked account. cloud-init only unlocks when a
            # password is set — random, never used (login is key-only).
            "cipassword": secrets.token_urlsafe(12),
        }
        for i, t in enumerate(team_ids):
            cfg[f"ipconfig{i + 1}"] = f"ip=192.168.{t}.1/24"
        # The alpine base template carries its BUILD-TIME cicustom user-data (its own
        # seeded key); it overrides the generated ciuser/sshkeys fragment, so the clone
        # would come up trusting the builder's key instead of the deploy key. Strip it
        # before first boot — the generated fragment is everything the jump needs.
        try:
            proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config", data={"delete": "cicustom"})
        except Exception as e:
            _log(f"    cicustom strip on {vmid} failed (template may not carry one): "
                  f"{str(e)[:100]}")
        proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config", data=cfg)

        # First start can outlive start_vm's budget on a busy host (live: qmstart on
        # the satellite took >120s) — tolerate a timed-out-but-running start.
        try:
            start_vm(node, vmid, timeout=300)
        except RuntimeError:
            if vm_status(node, vmid) != "running":
                raise
            _log("    start task outlived its wait — VM is running anyway")
        if not wait_for_guest_agent(node, vmid, timeout=180):
            _log("    WARNING: jump agent not answering (cloud-init may still be running)")
    user = ctx.get("vm_username", "sysadmin")
    # Every fresh clone regenerates its sshd host keys — the PREVIOUS jump's pinned
    # key for this mgmt IP makes accept-new refuse everything ("host key changed").
    # This was the real wait_jump failure across all earlier attempts: manual SSH
    # with /dev/null known_hosts worked minutes into the same boot.
    forget_engine_host_key(sat["jump_mgmt_ip"])
    if not wait_jump_ready(node, vmid, sat["jump_mgmt_ip"], user, ctx["ssh_key_path"],
                           os.environ["TF_VAR_ssh_public_key"]):
        raise RuntimeError(f"jump {vm_name} on '{node}' never came up on SSH at "
                           f"{sat['jump_mgmt_ip']} — check its console")
    configure_jump(sat["jump_mgmt_ip"], user, ctx["ssh_key_path"], team_ids, engine_mgmt_ip)
    _log(f"    jump-{comp_name}-{sat['slot']} configured and forwarding "
          f"(teams {', '.join(team_ids)})")


def destroy_jump_vms(placement, comp_tags, allow_untagged=False):
    """Phase-1/teardown half: destroy every satellite's jump VM (ownership-checked)."""
    from nodes_ops import record_of
    for sat in placement.get("satellites", []):
        rec = record_of(placement, sat["name"])
        try:
            destroy_vm_if_exists(rec.node, sat["jump_vmid"], expect_tags=comp_tags | {JUMP_TAGS_EXTRA},
                                 allow_untagged=allow_untagged)
        except RuntimeError as e:
            print(f"    WARNING: jump vmid {sat['jump_vmid']} on '{rec.node}': {e}")
