"""Competition configuration, team/box prompts, and inject handling."""

import contextlib
import json
import os
import re
import secrets
import string
import subprocess
import sys
from pathlib import Path

import requests

from constants import (ENGINE_TEMPLATE_VMID_OFFSET, GOLDEN_VMID_OFFSET,
                       MAX_BOXES_PER_TEAM, NAKON_DIR, SCORING_ENGINE_VMID)
from range_ops import has_clone_marker, parse_vm_tags, proxmox_api, proxmox_request, vm_id_for
from utils import (BOX_USERNAME_DEFAULT, CREDLIST_USERNAMES_DEFAULT, is_legacy_account_name,
                   valid_unix_username)

ENV_PATH = Path(".env")


def load_packet_passwords(comp_dir):
    """Packet-published credentials from passwords.json (compile-packet.py output), or None.

    Present means the deploy uses the packet's default credentials verbatim (that IS the
    competition: teams get them in the packet and rotate at minute zero) instead of
    minting random ones. 0600 + gitignored — future profiles may carry private secrets."""
    path = Path(comp_dir) / "passwords.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise SystemExit(f"  ERROR: {path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"  ERROR: {path} must be a JSON object")
    return data


def _catalog_check_paths(comp_dir):
    """Paths for `nakon catalog check`, filtered when the bundle uses constructs the
    catalog can't know about. Score-only pins ({"score_only": true}) name no catalog
    config; box_baseline.json pins plant like vulns but live in their own file. When
    neither is present the bundle's real files pass straight through. Returns
    (services_path, vulns_path, cleanup_fn)."""
    services = json.loads((comp_dir / "box_services.json").read_text())
    vulns = json.loads((comp_dir / "box_vulns.json").read_text())
    baseline_path = comp_dir / "box_baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}

    def _is_score_only(c):
        return isinstance(c, dict) and c.get("score_only")

    filtered_services = {box: [p for p in pins if not _is_score_only(p)]
                         for box, pins in services.items()}
    merged_vulns = {box: list(v) + list(baseline.get(box, []))
                    for box, v in vulns.items()}
    if filtered_services == services and merged_vulns == vulns:
        return (comp_dir / "box_services.json", comp_dir / "box_vulns.json", None)

    tmp_services = comp_dir / ".catalog-check-services.json"
    tmp_vulns = comp_dir / ".catalog-check-vulns.json"
    tmp_services.write_text(json.dumps(filtered_services, indent=2))
    tmp_vulns.write_text(json.dumps(merged_vulns, indent=2))

    def cleanup():
        tmp_services.unlink(missing_ok=True)
        tmp_vulns.unlink(missing_ok=True)

    return tmp_services, tmp_vulns, cleanup


def check_datastore_headroom(node, datastore, boxes, num_teams):
    """Blocking datastore-headroom gate: provisioned team-disk math vs pool avail."""
    # The storage LIST zeroes/omits free on some pools (cyberfield hdrives-zfs);
    # the per-store STATUS endpoint's avail is the authoritative number.
    try:
        st = proxmox_api("GET", f"/nodes/{node}/storage/{datastore}/status")["data"]
        free = st.get("avail")
    except Exception:
        free = None
    if free is None:
        print(f"  WARNING: could not read free space on datastore '{datastore}' — "
              f"headroom unchecked")
        return
    free_gb = free / 1024 ** 3
    # disk_gb unset means "template's own disk", unknowable here; 40 GB is a
    # conservative stand-in across the base templates.
    need_gb = num_teams * sum(b.get("disk_gb") or 40 for b in boxes)
    # On thin-provisioned pools (ZFS, lvmthin) linked clones only allocate written
    # blocks — the goldens and engine are the only full copies. Opt-in factor
    # (0 < TEZ_THIN_HEADROOM <= 1) counts that fraction of the provisioned math
    # instead of rejecting comps the pool can actually hold.
    thin = float(os.environ.get("TEZ_THIN_HEADROOM") or 1.0)
    if not 0 < thin <= 1:
        raise SystemExit(f"  ERROR: TEZ_THIN_HEADROOM must be in (0, 1], got {thin}")
    counted_gb = need_gb * thin
    if free_gb < counted_gb:
        raise SystemExit(
            f"  ERROR: datastore '{datastore}' has {free_gb:.0f} GB free; this deploy "
            f"needs ~{need_gb:.0f} GB ({num_teams} teams x {len(boxes)} boxes, unset disk "
            f"sizes counted as 40 GB"
            + (f", x{thin} thin factor" if thin != 1.0 else "")
            + "). Free space or trim the competition first.")
    print(f"  Preflight: datastore '{datastore}' {free_gb:.0f} GB free vs "
          f"~{counted_gb:.0f} GB needed"
          + (f" (provisioned ~{need_gb:.0f} GB x{thin} thin)" if thin != 1.0 else ""))


_CLOUDINIT_DISK_RE = re.compile(r"^(?:ide|scsi|sata)\d+$")


def template_cloudinit_missing(config):
    """True when a Linux-ostype VM config carries no cloud-init drive.

    A template can be tagged `template` and still be cloud-init-less — .150 vmid 920 is
    literally named `base-debian13-cloudinit` and ships none — so its linked clones boot
    with no network and no identity and the failure only surfaces an hour later at
    wait_for_boxes_ssh. Windows ostypes never carry one (identity comes from
    bootstrap_windows_box), so they and every other non-Linux ostype are exempt."""
    ostype = str((config or {}).get("ostype") or "").strip().lower()
    if not ostype.startswith("l"):
        return False
    for key, value in (config or {}).items():
        if _CLOUDINIT_DISK_RE.match(str(key)) and "cloudinit" in str(value).lower():
            return False
    return True


def _cloudinit_gate(tagged_by_name, boxes, label=""):
    """Refuse a selected Linux template that has no cloud-init drive.

    `tagged_by_name` maps template name -> cluster-resource entry (vmid + node). A config
    read that fails is warned about, not fatal — a transient API error must not block a
    deploy, but the template's cloud-init drive stays marked UNVERIFIED. Returns the
    number of distinct templates whose config was actually read and checked."""
    seen = set()
    verified = 0
    for box in boxes:
        if box.get("unmanaged"):
            continue  # unmanaged boxes get no cloud-init identity (terraform skips the block)
        name = box.get("template")
        vm = tagged_by_name.get(name)
        if vm is None or name in seen:
            continue
        seen.add(name)
        vmid, node = vm.get("vmid"), vm.get("node")
        try:
            config = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
        except Exception as e:
            print(f"  WARNING: could not read template '{name}' (vmid {vmid}) config — its "
                  f"cloud-init drive is UNVERIFIED ({str(e)[:80]})")
            continue
        if template_cloudinit_missing(config):
            fix = name if str(name).endswith("-fix") else f"{name}-fix"
            raise SystemExit(
                f"  ERROR: box template '{name}' (vmid {vmid}, node {node}, ostype "
                f"{config.get('ostype') or '?'}) is Linux but has no cloud-init drive — "
                f"its linked clones would boot with no network or identity and fail an "
                f"hour later at wait_for_boxes_ssh. Use a cloud-init-capable template "
                f"(e.g. the '{fix}' variant) or add a cloud-init drive to it.")
        verified += 1
    if verified:
        print(f"  Preflight{label}: {verified} box template(s) carry a cloud-init drive")
    return verified


def _gate_concurrent_deploys():
    """Refuse to start while another deploy is live on this host.

    The locks directory is the one concurrency signal that cannot lie: a holder is a
    live process, and the kernel releases the flock when it dies, so a leftover `.lock`
    file is never a false positive (unlike a mtime, a running VM, or a log). Two sessions
    sharing one estate is the documented cause of the 13xx vmid races, the foreign-golden
    squat and the over-broad sweep that took out two competitions' engines and goldens
    (AGENTS.md, docs/environment-facts.md).

    The signal is only meaningful because of the call order in `deploy.prepare()`:
    `_apply_engine_placement` takes the engine lock (line ~661) BEFORE
    `_run_competition_preflight` runs this gate (line ~669). Any deploy that has got far
    enough to matter is therefore holding a lock. The only uncovered window is the few
    seconds a second process spends loading config before it takes its own lock.

    TEZ_ALLOW_CONCURRENT=1 proceeds anyway — for a deliberately coordinated second range
    with its own vmid blocks, which is the only safe way to run two at once.
    """
    from nakon_ops import other_deploys_in_flight

    in_flight = other_deploys_in_flight()
    if not in_flight:
        return
    summary = ", ".join(f"{Path(path).name} ({int(age)}s)" for path, age in in_flight[:4])
    if len(in_flight) > 4:
        summary += f", +{len(in_flight) - 4} more"
    if os.environ.get("TEZ_ALLOW_CONCURRENT"):
        print(f"  WARNING: {len(in_flight)} other deploy(s) in flight on this host "
              f"({summary}) — proceeding because TEZ_ALLOW_CONCURRENT is set. Give this "
              f"range its own vmid blocks.")
        return
    raise SystemExit(
        f"  ERROR: {len(in_flight)} other deploy(s) are already running against this host "
        f"({summary}). Two concurrent deploys race for the same vmid blocks and golden "
        f"slots — the recorded outcome is a foreign template squatting this competition's "
        f"golden slot on the next run, and one sweep destroying another range's engines "
        f"(AGENTS.md). Wait for them (pgrep -af 'create-competition|redeploy-competition'), "
        f"or set TEZ_ALLOW_CONCURRENT=1 once you have coordinated distinct "
        f"--scoring-vmid / TF_VAR_team_identifiers blocks."
    )


def preflight_gates(comp_dir, boxes, num_teams, teams=None,
                    engine_vmid=SCORING_ENGINE_VMID, check_free=True,
                    engine_mgmt_ip=None, our_run_tag=None):
    """Blocking pre-apply gates: template resolution, vmid/bridge collisions,
    datastore headroom, catalog check.

    Each previously surfaced as a mid-deploy corpse (docs/e2e-testing.md §6): a
    missing template died inside terraform apply, a full datastore died mid-clone,
    a bad pin died mid-plant. The collision gate (check_free) makes concurrent
    competitions on one node safe: it fails fast if this comp's engine vmid, any
    team vmid, or any team bridge already exists (i.e. belongs to another range).
    our_run_tag (the prior state's run id) scopes the "ours" exemption to THIS
    deploy's lineage — a same-comp VM without it is another run's and stays a
    collision (2026-10-02 near-miss)."""
    node = os.environ["TF_VAR_proxmox_node"]
    _gate_concurrent_deploys()
    try:
        vms = proxmox_api("GET", "/cluster/resources", params={"type": "vm"})["data"]
    except Exception as e:
        # The API port IS the node's liveness signal. A site outage once left the tailnet
        # bridge answering ICMP *for itself* while forwarding nothing, so "ping works"
        # recovered hours before any TCP did (2026-09-24, ~10h outage) — never diagnose a
        # node from ping. `curl -k https://<node>:8006/` returning any HTTP code but 000
        # is the check that means something.
        raise SystemExit(
            f"  ERROR: Proxmox API unreachable during preflight: {e}\n"
            f"         This is the liveness check that counts — do NOT judge the node by "
            f"ping: during the 2026-09-24 outage the bridge answered ICMP while forwarding "
            f"nothing. Probe the API port directly (curl -k https://<node>:8006/ — any "
            f"HTTP code except 000 is up). Deploy state survives an outage; on recovery "
            f"resume with --from-phase rather than restarting.")
    tagged = set()
    tagged_by_name = {}
    for vm in vms:
        if vm.get("template") == 1 and "template" in (vm.get("tags") or "").split(";"):
            tagged.add(vm.get("name"))
            tagged_by_name.setdefault(vm.get("name"), vm)
    missing = sorted({b["template"] for b in boxes} - tagged)
    if missing:
        raise SystemExit(
            "  ERROR: box template(s) with no tagged template on the cluster: "
            + ", ".join(missing)
            + ". Clones would fail mid-apply; available: "
            + (", ".join(sorted(t for t in tagged if t)) or "(none)"))
    _cloudinit_gate(tagged_by_name, boxes)
    engine_base = int(os.environ["TF_VAR_template_vm_id"])
    if not any(vm.get("vmid") == engine_base for vm in vms):
        raise SystemExit(
            f"  ERROR: engine base image vmid {engine_base} (TF_VAR_template_vm_id) does "
            f"not exist on this cluster — the engine-template build and the scoring "
            f"engine clone would fail mid-apply.")
    print(f"  Preflight: all {len(boxes)} box template(s) resolve; engine base image vmid "
          f"{engine_base} present")

    # "Ours" = this deploy's full ownership set (comp tag + run id). Used by both the
    # vmid clash gate and the mgmt-IP gate; without a run tag a same-comp VM is
    # another run's and must classify as a collision, not as ours.
    our_tags = {"tezcatlipoca", f"comp-{comp_dir.name}"}
    if our_run_tag:
        our_tags.add(our_run_tag)

    if check_free and teams:
        existing_vmids = {vm.get("vmid") for vm in vms}
        vm_by_vmid = {vm.get("vmid"): vm for vm in vms}
        # A retry of THIS competition's failed deploy meets its own leftovers. A VM tagged
        # tezcatlipoca + comp-<name> + this deploy's run id is ours by creation (main.tf /
        # clone_ops / golden_ops all tag); phase 1's ownership-checked cleanup destroys it.
        # A same-comp VM MISSING the run id belongs to a different run (another worktree's,
        # or a pre-run-id deploy) and is a collision — the refusal message says which.

        def _is_ours(vmid, expected_name=None):
            vm = vm_by_vmid.get(vmid)
            if vm is None:
                return False
            vtags = {t.strip() for t in str(vm.get("tags") or "").replace(";", ",").split(",") if t.strip()}
            if our_tags <= vtags:
                return True
            # Interrupted clone: untagged (the tag PUT never ran) but carrying the clone
            # marker written in the clone POST itself — ours; phase 1 unlocks + destroys it.
            if not vtags and has_clone_marker(node, vmid, comp_dir.name):
                print(f"  Preflight: vmid {vmid} is an interrupted clone of this competition "
                      f"(lock={vm.get('lock') or 'none'}) — phase 1 will clean it")
                return True
            # Legacy M4 golden slots predate ownership tags. Adopt only the exact
            # reserved golden VM/name pair; all other untagged resources remain foreign.
            return (expected_name is not None and vm.get("name") == expected_name
                    and vtags == {"template"})

        clashes = []
        foreign = 0
        ours = 0

        def _clash(label, vmid, expected_name=None):
            nonlocal foreign, ours
            if _is_ours(vmid, expected_name=expected_name):
                ours += 1
            else:
                foreign += 1
                clashes.append(label)

        if engine_vmid in existing_vmids:
            _clash(f"scoring engine vmid {engine_vmid}", engine_vmid)
        # M4: the engine template's reserved slot (just below the golden block). A VM
        # there tagged as ours is the competition's persistent template — expected to
        # survive phase 1 and be reused, NOT a leftover to clean. Foreign = fatal.
        et_vmid = engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET
        if et_vmid in existing_vmids:
            if _is_ours(et_vmid):
                print(f"  Preflight: engine template vmid {et_vmid} present (M4 persistent — reused)")
            else:
                foreign += 1
                clashes.append(f"engine template vmid {et_vmid}")
        for team_key, team in teams.items():
            for box_idx in range(len(boxes)):
                vid = vm_id_for(team["identifier"], box_idx)
                if vid in existing_vmids:
                    _clash(f"team vmid {vid} ({team_key}/{boxes[box_idx]['name']})", vid)
        for box_idx in range(len(boxes)):
            vid = engine_vmid + 150 + box_idx
            if vid in existing_vmids:
                _clash(f"golden vmid {vid} ({boxes[box_idx]['name']})", vid,
                       expected_name=f"golden-{boxes[box_idx]['name']}")
        try:
            nets = proxmox_api("GET", f"/nodes/{node}/network")["data"]
            existing_bridges = {n.get("iface") for n in nets}
        except Exception:
            existing_bridges = set()
        # In-path firewalls add a per-team transit bridge (vmbrW<id>) alongside the
        # team bridge — same collision rules, same "ours" tolerance.
        wants_fw = any(b.get("in_path") for b in boxes)
        for team in teams.values():
            for prefix in ("vmbr", "vmbrW") if wants_fw else ("vmbr",):
                bridge = f"{prefix}{team['identifier']}"
                # Bridges carry no per-comp tags. Tolerate one only when the VM leftovers are
                # unambiguously all ours (a retry) — a foreign bridge stays fatal.
                if bridge in existing_bridges:
                    if foreign == 0 and ours > 0:
                        ours += 1
                    else:
                        clashes.append(f"bridge {bridge}")
        if clashes:
            other_run = any(
                f"comp-{comp_dir.name}" in str(vm.get("tags") or "")
                and our_run_tag and our_run_tag not in parse_vm_tags(vm.get("tags"))
                for vm in vm_by_vmid.values())
            raise SystemExit(
                "  ERROR: this competition's infrastructure collides with VMs/bridges already "
                "on node '" + node + "' (another running competition?): " + ", ".join(clashes)
                + ". Pick a free --scoring-vmid and/or non-overlapping TF_VAR_team_identifiers. "
                "Note the golden block sits at <scoring-vmid>+150 — a colliding golden vmid "
                "also means picking a different engine vmid."
                + (" The colliding VM(s) carry this competition's comp tag but NOT this "
                   "run's run-id tag: a PRE-RUN-ID deploy of this competition (tear it down "
                   "with destroy-competition.py --legacy-tags --yes first) or ANOTHER "
                   "worktree's run of the same competition ID (coordinate with its session "
                   "— never destroy it from here)." if other_run else ""))
        if ours:
            print(f"  Preflight: {ours} leftover VM(s)/bridge(s) tagged as this "
                  f"competition's — phase 1 cleans or (M4 hash-matching templates) reuses them")
        else:
            print(f"  Preflight: engine vmid {engine_vmid}, engine template vmid {et_vmid}, "
                  f"golden vmids {engine_vmid + 150}+, all team vmids, and team bridges are free")

    datastore = os.environ.get("TF_VAR_datastore", "local-lvm")
    check_datastore_headroom(node, datastore, boxes, num_teams)

    if engine_mgmt_ip:
        _engine_mgmt_ip_gate(node, vms, engine_vmid, engine_mgmt_ip, ours_tags=our_tags)
    _catalog_gate(comp_dir)


def _engine_mgmt_ip_gate(node, vms, engine_vmid, engine_mgmt_ip, ours_tags=None):
    """Static engine mgmt IP must not collide with any running guest on the mgmt L2.

    Two things this gate got wrong, both live-found on the 2026-10-02 same-type-2box
    practice run — where it printed "engine mgmt IP 10.0.0.250 is free" while a FOREIGN
    live `quotient-engine` was answering that exact address:

    * **The mgmt L2 spans nodes.** The scan did `vm["node"] != node: continue`, but
      10.0.0.0/24 is one flat segment: the foreign engine was on .193 and this deploy
      was on .150. The data was already cluster-wide (`/cluster/resources`) — the filter
      threw away the rows that mattered. The scan is now cluster-wide.
    * **Unverifiable is not free.** A guest whose agent is down was counted and skipped,
      and the gate then claimed the address was free from an absence of evidence. When
      any running guest cannot be checked the address is *unknown*: refusing is correct
      for the DEFAULT ip (it is what every comp gets, and nobody chose it), while an IP
      the operator set explicitly gets a loud warning instead, because they have taken
      responsibility for it.

    Guests without a working agent can't be checked; count them out loud instead of
    claiming the range is clean."""
    unchecked, taken, takers = [], set(), {}
    for vm in vms:
        if vm.get("status") != "running" or vm.get("template") == 1:
            continue
        if vm.get("vmid") == engine_vmid:
            # Our own engine from a prior failed attempt — it holds the planned
            # mgmt IP until phase 1 destroys it seconds from now (live-found
            # 2026-09-29: every retry after a phase-2+ failure re-tripped this
            # gate against our own leftover).
            continue
        # Broader than the vmid check: ANY VM tagged as this competition's (a
        # leftover box, not just the engine) answers the IP only until phase 1
        # recycles it — only a FOREIGN guest squatting the address is a collision.
        if ours_tags:
            vm_tags = {t.strip() for t in str(vm.get("tags") or "")
                       .replace(";", ",").split(",") if t.strip()}
            if ours_tags <= vm_tags:
                continue
        label = f"{vm.get('vmid')} ({vm.get('name') or '?'} on {vm.get('node') or '?'})"
        try:
            result = proxmox_api(
                "GET", f"/nodes/{vm['node']}/qemu/{vm['vmid']}/agent/network-get-interfaces"
            )["data"]["result"]
        except Exception:
            unchecked.append(label)
            continue
        for ifc in result or []:
            for addr in ifc.get("ip-addresses") or []:
                if addr.get("ip-address-type") == "ipv4":
                    taken.add(addr.get("ip-address"))
                    takers.setdefault(addr.get("ip-address"), label)
    if engine_mgmt_ip in taken:
        raise SystemExit(
            f"  ERROR: static engine mgmt IP {engine_mgmt_ip} is already answered by a "
            f"running guest ({takers.get(engine_mgmt_ip, 'unknown')}). Pick another "
            f"TF_VAR_engine_mgmt_ip (or set it to '' for DHCP). The management network is "
            f"shared across nodes, so a guest on ANY node counts.")
    # "Explicitly set" means the OPERATOR set it — not that the code exported the
    # default into the same variable seconds ago (deploy._engine_mgmt_ip_from_env).
    # A default IP the preflight cannot verify is refused outright: the .150 default
    # (.250) drops SSH mid-build on the tailnet path (2026-10-03), and an unverifiable
    # default was a live foreign engine's address once before that (2026-10-02).
    explicit = (bool((os.environ.get("TF_VAR_engine_mgmt_ip") or "").strip())
                and not os.environ.get("TEZ_ENGINE_MGMT_IP_IS_DEFAULT"))
    if unchecked:
        listing = ", ".join(unchecked[:5]) + ("..." if len(unchecked) > 5 else "")
        if not explicit:
            raise SystemExit(
                f"  ERROR: cannot verify the engine mgmt IP {engine_mgmt_ip} — "
                f"{len(unchecked)} running guest(s) have no working agent to ask "
                f"({listing}). This is the DEFAULT address, and on this estate the "
                f"default has already been a live foreign engine's address once "
                f"(2026-10-02). Set TF_VAR_engine_mgmt_ip to an address you have checked "
                f"yourself (or '' for DHCP). On this estate the default (.250) also "
                f"drops SSH mid-build on the .150 tailnet path (2026-10-03) — the "
                f"cyberrange env variants pin 10.0.0.252 instead.")
        print(f"  Preflight: engine mgmt IP {engine_mgmt_ip} — UNVERIFIED "
              f"({len(unchecked)} running guest(s) have no agent: {listing}); proceeding "
              f"because TF_VAR_engine_mgmt_ip is set explicitly")
        return
    print(f"  Preflight: engine mgmt IP {engine_mgmt_ip} is free")


def _catalog_gate(comp_dir):
    svc_path, vuln_path, cleanup = _catalog_check_paths(comp_dir)
    catalog = subprocess.run(
        [sys.executable, "-m", "nakon", "catalog", "check",
         "--boxes-json", str((comp_dir / "boxes.json").resolve()),
         "--box-services", str(svc_path.resolve()),
         "--box-vulns", str(vuln_path.resolve())],
        cwd=str(NAKON_DIR), capture_output=True, text=True, timeout=600)
    if cleanup:
        cleanup()
    if catalog.returncode != 0:
        print(catalog.stdout)
        print(catalog.stderr)
        raise SystemExit(
            "  ERROR: nakon catalog check reported errors for this competition's pins — "
            "fix or trim box_vulns.json/box_services.json before deploying (details above).")
    print("  Preflight: nakon catalog check 0 errors")


def preflight_gates_multinode(comp_dir, boxes, teams, engine_vmid, placement,
                              engine_mgmt_ip=None, check_free=True, our_run_tag=None):
    """Per-node preflight for a multi-node placement: every hosting node gets its own
    template/collision/headroom gate scoped to exactly what it will hold (engine node:
    engine base + slot-0 goldens + its teams; each satellite: its slot's goldens, the
    jump VM, its teams). Same failure classes as preflight_gates, caught per node —
    a satellite that would have died mid-clone on another host fails here instead."""
    from jump_ops import find_jump_template
    from nodes_ops import record_of, teams_on_node
    from range_ops import cluster_vms_for

    comp_name = comp_dir.name
    engine_name = placement["engine_node"]
    # In-path firewalls are a slot-0 feature for now: the transit bridge + gateway
    # handoff live on the engine node, while each satellite's jump VM impersonates
    # 192.168.<id>.1 for its local teams — an in-path firewall there would fight the
    # jump for the gateway address (docs/multi-node.md).
    if any(b.get("in_path") for b in boxes) and any(
            s != 0 for s in placement.get("team_slots", {}).values()):
        raise SystemExit(
            "  ERROR: in_path firewalls are only supported on engine-node (slot 0) teams — "
            "the satellite design gives 192.168.<id>.1 to the jump VM, so an in-path "
            "firewall there would fight it for the gateway address. Move the teams to the "
            "engine node or drop the firewall from the lineup.")
    # Full ownership set (comp tag + run id) — see preflight_gates. Hoisted above the
    # per-node loop so the engine-node mgmt-IP gate after the loop can reuse it.
    our_tags = {"tezcatlipoca", f"comp-{comp_name}"}
    if our_run_tag:
        our_tags.add(our_run_tag)
    for node_name in placement["slots"]:
        rec = record_of(placement, node_name)
        slot = placement["slots"][node_name]
        node = rec.node
        node_team_keys = teams_on_node(placement, node_name)
        node_teams = {k: teams[k] for k in node_team_keys}
        vms = cluster_vms_for(node)
        node_vms = [vm for vm in vms if vm.get("node") == node]

        # Template resolution scoped to this node (its clones can only use its own
        # templates): engine base on the engine node, every box template on any
        # node hosting teams, and a jump clone source on satellites.
        if node_name == engine_name:
            base = rec.engine_base_vmid
            if not any(vm.get("vmid") == base for vm in node_vms):
                raise SystemExit(
                    f"  ERROR: engine base image vmid {base} does not exist on engine "
                    f"node '{node_name}' ({node}) — the engine-template build would fail.")
        if node_teams:
            tagged = set()
            tagged_by_name = {}
            for vm in node_vms:
                if vm.get("template") == 1 and "template" in (vm.get("tags") or "").split(";"):
                    tagged.add(vm.get("name"))
                    tagged_by_name.setdefault(vm.get("name"), vm)
            missing = sorted({b["template"] for b in boxes} - tagged)
            if missing:
                raise SystemExit(
                    f"  ERROR: box template(s) with no tagged template on node "
                    f"'{node_name}' ({node}): " + ", ".join(missing)
                    + f". Available there: {sorted(t for t in tagged if t) or '(none)'}. "
                      "Sync with sync-template.py or move the teams.")
            _cloudinit_gate(tagged_by_name, boxes, label=f"[{node_name}]")
            if slot > 0:
                find_jump_template(node, rec.jump_template)  # raises with remedy
                print(f"  Preflight[{node_name}]: box templates + jump clone source present")

        if not node_teams and slot > 0:
            continue  # unused satellite — nothing else to check there

        if not check_free:
            continue  # resume: this competition's own leftovers are phase-1/terraform's to reconcile

        # Collision gate scoped to this node's share ("ours" = our_tags, hoisted above).
        existing_vmids = {vm.get("vmid") for vm in node_vms}
        vm_by_vmid = {vm.get("vmid"): vm for vm in node_vms}

        def _is_ours(vmid, expected_name=None):
            vm = vm_by_vmid.get(vmid)
            if vm is None:
                return False
            vtags = {t.strip() for t in str(vm.get("tags") or "").replace(";", ",").split(",") if t.strip()}
            if our_tags <= vtags:
                return True
            if not vtags and has_clone_marker(node, vmid, comp_name):
                return True
            return (expected_name is not None and vm.get("name") == expected_name
                    and vtags == {"template"})

        clashes, foreign, ours = [], 0, 0

        def _clash(label, vmid, expected_name=None):
            nonlocal foreign, ours
            if _is_ours(vmid, expected_name=expected_name):
                ours += 1
            else:
                foreign += 1
                clashes.append(label)

        checked = []
        if node_name == engine_name:
            checked.append((engine_vmid, f"scoring engine vmid {engine_vmid}", None))
            et_vmid = engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET
            checked.append((et_vmid, f"engine template vmid {et_vmid}", None))
        for box_idx in range(len(boxes)):
            checked.append((engine_vmid + GOLDEN_VMID_OFFSET + slot * MAX_BOXES_PER_TEAM + box_idx,
                            f"golden vmid (slot {slot}) #{engine_vmid + GOLDEN_VMID_OFFSET + slot * MAX_BOXES_PER_TEAM + box_idx}",
                            f"golden-{boxes[box_idx]['name']}"))
        if slot > 0:
            checked.append((engine_vmid + 130 + slot, f"jump vmid {engine_vmid + 130 + slot}",
                            f"jump-{comp_name}-{slot}"))
        for team_key, team in node_teams.items():
            for box_idx in range(len(boxes)):
                vid = vm_id_for(team["identifier"], box_idx)
                checked.append((vid, f"team vmid {vid} ({team_key}/{boxes[box_idx]['name']})", None))
        for vmid, label, expected_name in checked:
            if vmid in existing_vmids:
                _clash(label, vmid, expected_name)

        try:
            nets = proxmox_api("GET", f"/nodes/{node}/network")["data"]
            existing_bridges = {n.get("iface") for n in nets}
        except Exception:
            existing_bridges = set()
        wants_fw = any(b.get("in_path") for b in boxes)
        node_bridges = [f"vmbr{t['identifier']}" for t in node_teams.values()]
        if wants_fw and slot == 0:  # transit bridges are engine-node resources (slot-0 teams)
            node_bridges += [f"vmbrW{t['identifier']}" for t in node_teams.values()]
        for bridge in node_bridges:
            if bridge in existing_bridges:
                if foreign == 0 and ours > 0:
                    ours += 1
                else:
                    clashes.append(f"bridge {bridge}")
        if clashes:
            other_run = any(
                f"comp-{comp_name}" in str(vm.get("tags") or "")
                and our_run_tag and our_run_tag not in parse_vm_tags(vm.get("tags"))
                for vm in vm_by_vmid.values())
            raise SystemExit(
                f"  ERROR: this competition's infrastructure collides with VMs/bridges "
                f"already on node '{node_name}' ({node}): " + ", ".join(clashes)
                + ". Pick a free --scoring-vmid and/or non-overlapping team identifiers."
                + (" The colliding VM(s) carry this competition's comp tag but NOT this "
                   "run's run-id tag: a PRE-RUN-ID deploy of this competition (tear it "
                   "down with destroy-competition.py --legacy-tags --yes first) or "
                   "ANOTHER worktree's run of the same competition ID (coordinate with "
                   "its session — never destroy it from here)." if other_run else ""))
        if ours:
            print(f"  Preflight[{node_name}]: {ours} leftover VM(s)/bridge(s) tagged as "
                  f"this competition's — phase 1 cleans or reuses them")

        # Headroom scoped to this node's share.
        datastore = rec.datastore
        try:
            st = proxmox_api("GET", f"/nodes/{node}/storage/{datastore}/status")["data"]
            free = st.get("avail")
        except Exception:
            free = None
        if free is None:
            print(f"  Preflight[{node_name}]: WARNING could not read free space on "
                  f"'{datastore}' — headroom unchecked")
        else:
            free_gb = free / 1024 ** 3
            need_gb = len(node_teams) * sum(b.get("disk_gb") or 40 for b in boxes)
            # Same thin-pool factor as the single-node headroom gate: linked clones
            # on ZFS/lvmthin only allocate written blocks (TEZ_THIN_HEADROOM, 0<x<=1).
            thin = float(os.environ.get("TEZ_THIN_HEADROOM") or 1.0)
            if not 0 < thin <= 1:
                raise SystemExit(f"  ERROR: TEZ_THIN_HEADROOM must be in (0, 1], got {thin}")
            counted_gb = need_gb * thin
            if free_gb < counted_gb:
                raise SystemExit(
                    f"  ERROR: datastore '{datastore}' on '{node_name}' has {free_gb:.0f} GB "
                    f"free; this deploy needs ~{need_gb:.0f} GB there "
                    f"({len(node_teams)} teams x {len(boxes)} boxes"
                    + (f", x{thin} thin factor" if thin != 1.0 else "")
                    + "). Free space or trim the competition first.")
            print(f"  Preflight[{node_name}]: datastore '{datastore}' {free_gb:.0f} GB free "
                  f"vs ~{counted_gb:.0f} GB needed"
                  + (f" (provisioned ~{need_gb:.0f} GB x{thin} thin)" if thin != 1.0 else ""))

    # mgmt-IP sweeps: the engine IP on the engine node; every satellite's jump IP
    # against guests on ALL hosting nodes (they share one mgmt L2).
    engine_rec = record_of(placement, engine_name)
    if engine_mgmt_ip:
        node = engine_rec.node
        vms = cluster_vms_for(node)
        _engine_mgmt_ip_gate(node, vms, engine_vmid, engine_mgmt_ip, ours_tags=our_tags)
    jump_ips = [s["jump_mgmt_ip"] for s in placement["satellites"]]
    if jump_ips:
        taken, unchecked = set(), 0
        for name in placement["slots"]:
            node = record_of(placement, name).node
            for vm in cluster_vms_for(node):
                if vm.get("node") != node or vm.get("status") != "running" or vm.get("template") == 1:
                    continue
                if vm.get("vmid") in {engine_vmid} | {s["jump_vmid"] for s in placement["satellites"]}:
                    continue
                try:
                    result = proxmox_api(
                        "GET", f"/nodes/{node}/qemu/{vm['vmid']}/agent/network-get-interfaces"
                    )["data"]["result"]
                except Exception:
                    unchecked += 1
                    continue
                for ifc in result or []:
                    for addr in ifc.get("ip-addresses") or []:
                        if addr.get("ip-address-type") == "ipv4":
                            taken.add(addr.get("ip-address"))
        dupes = sorted(set(jump_ips) & taken)
        if dupes:
            raise SystemExit(
                f"  ERROR: jump mgmt IP(s) {dupes} already answered by a running guest "
                "on the mgmt LAN — set explicit jump_mgmt_ip values in nodes.json.")
        note = (f" ({unchecked} guest(s) unverifiable — agent down)" if unchecked else "")
        print(f"  Preflight: jump mgmt IP(s) {jump_ips} free{note}")

    _catalog_gate(comp_dir)


def load_previous_competitions():
   return [
      p.name
      for p in Path("competitions").iterdir()
      if p.is_dir() and (p / "Compfile").exists()
   ]


def random_password():
    """14 chars, guaranteed upper+lower+digit+symbol, cmd/PS- and URL-safe charset.

    Windows guest boxes set this via `net user` and AD enforces complexity:
    the old letters+digits pool produced digit-free passwords ~13% of runs
    (scrim-extreme-2026-09-20) and the policy rejection killed every Windows
    login downstream. Characters are cmd/PS-quoting safe (no &|<>^%$`"' or
    whitespace) and URL-grammar free (no #/?@): these secrets land in
    postgres DSNs in /opt/quotient/.env, and `#` silently truncates the DSN
    at the password (scrim-extreme-cyberfield-2026-09-22 crash loop).

    Single source of truth for every generated secret, including packet_ops' decoy
    baseline accounts (which used to carry a copy of this generator, audit 2026-10-02).
    Changing the charset or length changes behaviour everywhere at once — deliberately."""
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!*_-+="]
    alphabet = "".join(pools)
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(14))
        if all(any(c in p for c in pw) for p in pools):
            return pw


def collect_teams(number_of_teams, engine_vmid=SCORING_ENGINE_VMID):
    teams = {}
    import os as _os
    override = (_os.environ.get("TF_VAR_team_identifiers") or "").strip()
    ids = [s.strip() for s in override.split(",")] if override else [
        str(100 + i) for i in range(1, number_of_teams + 1)
    ]
    if override and len(ids) < number_of_teams:
        ids += [str(100 + i) for i in range(len(ids) + 1, number_of_teams + 1)]
    seen = set()
    for i in range(1, number_of_teams + 1):
        key = f"team{i}"
        identifier = ids[i - 1]
        if not (identifier.isdigit() and 1 <= int(identifier) <= 254):
            raise SystemExit(
                f"  ERROR: team identifier {identifier!r} must be an integer in 1..254 "
                f"(it becomes the 192.168.<id>.x subnet)."
            )
        if identifier in seen:
            raise SystemExit(f"  ERROR: duplicate team identifier {identifier} — subnets/vmids would collide.")
        seen.add(identifier)
        base = 200 + int(identifier) * 10
        # A team's vmid block must clear not just the engine but its DERIVED slots:
        # engine template (engine+140) and the golden block (engine+150..+159). The
        # old check covered the engine alone (live-found 2026-09-29: engine 1080 +
        # default identifiers put team2's first box vmid exactly on the engine
        # template slot at 1220 — the collision only surfaces at terraform apply #2,
        # hours into the deploy). Phase 1's wave logic treats all 10 slots per block,
        # so the guard is full-width.
        reserved = ({engine_vmid}
                    | set(range(engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET,
                                engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET + MAX_BOXES_PER_TEAM))
                    | set(range(engine_vmid + GOLDEN_VMID_OFFSET,
                                engine_vmid + GOLDEN_VMID_OFFSET + MAX_BOXES_PER_TEAM)))
        block = set(range(base, base + MAX_BOXES_PER_TEAM))
        if block & reserved:
            raise SystemExit(
                f"  ERROR: team identifier {identifier} maps to vmids "
                f"{base}..{base + MAX_BOXES_PER_TEAM - 1}, overlapping this engine's "
                f"reserved slots (engine {engine_vmid}, engine template "
                f"{engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET}, goldens "
                f"{engine_vmid + GOLDEN_VMID_OFFSET}+) — terraform apply #2 would "
                f"clone a team box onto one of them. Pick different "
                f"TF_VAR_team_identifiers or --scoring-vmid.")
        password = random_password()
        teams[key] = {"identifier": identifier, "password": password}
    return teams


def update_env(updates: dict):
    text = ENV_PATH.read_text()
    for key, value in updates.items():
        line = f"{key}={value}"
        new_text, count = re.subn(rf"^{re.escape(key)}=.*$", lambda _m: line, text, flags=re.MULTILINE)
        text = new_text if count else text + f"\n{line}\n"
        os.environ[key] = value
    write_text_atomic(ENV_PATH, text, mode=0o600)


def write_text_atomic(path, text, mode=0o600):
    """Write `text` to `path` so a torn write can never be observed, then chmod.

    Both halves matter and both have bitten this repo:

    *Atomicity* — `.deploy_state.json` holds the only copy of the generated box and
    team passwords; a partial write bricks resume AND redeploy at once (deploy.py
    documented this after the fact, but three other writers of the same file used a
    plain write_text, so the guarantee held at exactly one of four call sites).

    *Mode at creation* — creating with the process umask and chmod-ing afterwards
    leaves a window where the file is world-readable. os.open() applies the mode
    atomically with creation, so the secret is never briefly public.
    """
    path = Path(path)
    tmp_path = path.with_name(path.name + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    os.replace(tmp_path, path)


def write_state(path, state):
    """Persist a JSON state dict atomically, 0600. Single writer for .deploy_state.json."""
    write_text_atomic(path, json.dumps(state, indent=2), mode=0o600)


def list_proxmox_templates():
    endpoint = os.environ["TF_VAR_proxmox_endpoint"].rstrip("/")
    scoring_template_id = int(os.environ["TF_VAR_template_vm_id"])
    try:
        r = proxmox_request(
            "GET", f"{endpoint}/api2/json/cluster/resources",
            params={"type": "vm"},
            headers={"Authorization": f"PVEAPIToken={os.environ['TF_VAR_proxmox_api_token']}"},
            timeout=10,
        )
        r.raise_for_status()
        vms = r.json()["data"]
    except Exception as e:
        print(f"  (couldn't list Proxmox templates: {e})")
        return []
    return sorted(
        vm["name"] for vm in vms
        if vm.get("template") == 1
        and "template" in (vm.get("tags") or "").split(";")
        and vm.get("vmid") != scoring_template_id
    )


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


def _prompt_int(prompt, default):
    """int(input()) with a default on blank and a re-prompt (not a crash) on non-numeric input."""
    while True:
        raw = input(prompt).strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print(f"  Enter a whole number (or leave blank for {default}).")


def _prompt_difficulty():
    """Difficulty prompt that re-asks on non-numeric/out-of-range input instead of crashing."""
    while True:
        raw = input("Difficulty (1-10): ").strip()
        try:
            value = int(raw)
        except ValueError:
            print("  Enter a whole number from 1 to 10.")
            continue
        if 1 <= value <= 10:
            return value
        print("  Enter a number from 1 to 10.")


def _prompt_optional_int(prompt):
    """Like _prompt_int, but blank means None (no default) instead of a fallback value."""
    while True:
        raw = input(prompt).strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            print("  Enter a whole number, or leave blank.")


def collect_boxes():
    templates = list_proxmox_templates()
    if templates:
        print("Available box templates (tagged 'template' in Proxmox):")
        for i, t in enumerate(templates, 1):
            print(f"  [{i}] {t}")
        print()
    else:
        print("  (no templates found in Proxmox — you'll need to type template names manually)\n")

    while True:
        raw = input("How many box types for this competition? ").strip()
        try:
            number_of_boxes = int(raw)
        except ValueError:
            print("  Enter a whole number.")
            continue
        if 1 <= number_of_boxes <= MAX_BOXES_PER_TEAM:
            break
        print(f"  Enter a number from 1 to {MAX_BOXES_PER_TEAM} (see MAX_BOXES_PER_TEAM).")

    boxes = []
    for i in range(1, number_of_boxes + 1):
        print(f"\n─── Box {i} of {number_of_boxes} " + "─" * 40)

        existing_names = {b["name"] for b in boxes}
        name = input("  Name (e.g. web01, db, mail): ").strip()
        while not name or name in existing_names:
            if name in existing_names:
                name = input(f"  '{name}' is already used by another box in this competition"
                              " — pick a different name: ").strip()
            else:
                name = input("  Name can't be blank: ").strip()

        if templates:
            while True:
                raw = input(f"  Template [{1}–{len(templates)}, or name]: ").strip()
                try:
                    idx = int(raw)
                    if 1 <= idx <= len(templates):
                        template = templates[idx - 1]
                        break
                except ValueError:
                    pass
                if raw in templates:
                    template = raw
                    break
                print(f"  Enter a number from 1 to {len(templates)}, or a template name.")
        else:
            template = input("  Template name: ").strip()
            while not template:
                template = input("  Template can't be blank — must match a tagged Proxmox VM exactly: ").strip()

        cpu = _prompt_int("  CPU cores    [1]: ", 1)
        memory_mb = _prompt_int("  Memory (MB)  [2048]: ", 2048)
        disk_gb = _prompt_optional_int("  Disk (GB)    [keep template's]: ")

        box = {
            "name": name, "last_octet": i + 1, "cpu": cpu, "memory_mb": memory_mb,
            "disk_gb": disk_gb, "template": template,
        }
        boxes.append(box)
    return boxes


def collect_users_config(box_username_flag=None, credlist_flag=None):
    """Collect themeable box login + 3 credlist usernames; defaults when blank, always writes users.json."""

    if box_username_flag is not None:
        box_username = box_username_flag.strip() or BOX_USERNAME_DEFAULT
    else:
        box_username = input(f"  Box login username [{BOX_USERNAME_DEFAULT}]: ").strip() or BOX_USERNAME_DEFAULT
    if not valid_unix_username(box_username):
        print(f"  '{box_username}' is not a valid box username — using {BOX_USERNAME_DEFAULT}.")
        box_username = BOX_USERNAME_DEFAULT
    elif is_legacy_account_name(box_username):
        print(f"  '{box_username}' collides with a legacy distro system account (cloud-init "
              f"would adopt it and brick auth) — using {BOX_USERNAME_DEFAULT} instead.")
        box_username = BOX_USERNAME_DEFAULT

    if credlist_flag is not None:
        raw = credlist_flag
    else:
        raw = input(
            f"  Credlist usernames, comma-separated (exactly 3) "
            f"[{','.join(CREDLIST_USERNAMES_DEFAULT)}]: "
        ).strip()

    if raw:
        credlist_usernames = [n.strip() for n in raw.split(",") if n.strip()]
        if len(credlist_usernames) != 3:
            print(f"  Need exactly 3 credlist usernames — got {len(credlist_usernames)}, "
                  f"falling back to the default {CREDLIST_USERNAMES_DEFAULT}.")
            credlist_usernames = list(CREDLIST_USERNAMES_DEFAULT)
        elif not all(valid_unix_username(n) for n in credlist_usernames):
            print(f"  Credlist usernames must be lowercase [a-z_][a-z0-9_-]* — "
                  f"falling back to the default {CREDLIST_USERNAMES_DEFAULT}.")
            credlist_usernames = list(CREDLIST_USERNAMES_DEFAULT)
    else:
        credlist_usernames = list(CREDLIST_USERNAMES_DEFAULT)

    return box_username, credlist_usernames


def load_injects(comp_dir):
    """Load per-competition injects (title/description/offsets/attachments); [] when no injects/ dir."""
    injects_dir = comp_dir / "injects"
    if not injects_dir.is_dir():
        return []

    injects = []
    for sub in sorted(injects_dir.iterdir()):
        manifest = sub / "inject.json"
        if not sub.is_dir() or not manifest.exists():
            continue
        meta = json.loads(manifest.read_text())

        description = meta.get("description", "")
        if meta.get("description_file"):
            desc_path = sub / meta["description_file"]
            if desc_path.exists():
                description = desc_path.read_text()

        skip = {"inject.json", meta.get("description_file")}
        files = [str(f) for f in sorted(sub.iterdir()) if f.is_file() and f.name not in skip]

        injects.append({
            "title":       meta["title"],
            "description": description,
            "open_offset_min":  meta.get("open_offset_min", 0),
            "due_offset_min":  meta.get("due_offset_min", 60),
            "close_offset_min": meta.get("close_offset_min", 90),
            "files":       files,
        })
    return injects


def resolve_inject_times(injects):
    """Resolve inject offsets to RFC3339 timestamps anchored at now (phase 7). Mutates in place."""

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

    def rfc3339(minutes):
        return (now + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for inj in injects:
        inj["open_time"] = rfc3339(inj.pop("open_offset_min", 0))
        inj["due_time"] = rfc3339(inj.pop("due_offset_min", 60))
        inj["close_time"] = rfc3339(inj.pop("close_offset_min", 90))
    return injects


def injects_fingerprint(injects):
    """Stable identity of the inject *definitions*, for phase 7's done-marker.

    Call this BEFORE `resolve_inject_times` — that function pops the offset fields and
    rewrites them as absolute timestamps, so a fingerprint taken afterwards changes on
    every run and would re-create the whole set every time.

    `injects_created` used to be a bare boolean, which meant an inject added (or a
    window edited) after the first phase-7 run was silently skipped forever on resume:
    `create_injects` dedups on titles precisely so a re-run is safe, but nothing ever
    re-ran it. Titles are what that dedup keys on, so they lead the fingerprint; the
    offsets and attachment names are included so a changed window or a swapped
    attachment is not missed either.
    """
    import hashlib

    items = []
    for inj in injects or []:
        items.append({
            "title": inj.get("title"),
            "open_offset_min": inj.get("open_offset_min", 0),
            "due_offset_min": inj.get("due_offset_min", 60),
            "close_offset_min": inj.get("close_offset_min", 90),
            "attachments": sorted(
                (a.get("name") if isinstance(a, dict) else str(a)) or ""
                for a in (inj.get("attachments") or [])),
        })
    blob = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def load_boxes(comp_dir):
    path = comp_dir / "boxes.json"
    return json.loads(path.read_text()) if path.exists() else None


def confirm_deploy(name, scenario, difficulty, teams, boxes):
    n = len(teams)
    team_range = f"team1–team{n}" if n > 1 else "team1"
    short_scenario = scenario[:72] + ("..." if len(scenario) > 72 else "")

    print("\n─── Ready to deploy " + "─" * 44)
    print(f"  Competition : {name}")
    print(f"  Scenario    : {short_scenario}")
    print(f"  Difficulty  : {difficulty} / 10")
    print(f"  Teams       : {n}  ({team_range}, passwords auto-generated)")
    print("  Boxes       :")
    for b in boxes:
        print(f"    {b['name']} — {b['template']}  ({b['cpu']} CPU, {b['memory_mb']} MB)")
    print()
    print("  Terraform will now run; this takes several minutes.")
    answer = input("  Continue? (y/n): ").strip().lower()
    return answer == "y"
