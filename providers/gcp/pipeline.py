"""The GCP provider: Compute Engine ranges — one VPC per run, labels for ownership.

Mapping (docs/architecture.md keeps the full table):
- bridges            -> a per-run VPC with a subnet per team (+ mgmt/red/transit)
- team isolation     -> the engine as the subnet .1 router (iptables) + GCP's implied
                        cross-subnet deny; egress rides Cloud NAT
- PVE tags/marker     -> labels on every instance/image (`naming`); creates are atomic,
                        so the interrupted-clone marker class has no analog
- qm template / clone -> custom images; every team box boots its golden image
- tz snapshots        -> machine images per box (tz-base / tz-ready); GCP requires the
  instance stopped for a machine image, so taking a restore point reboots the box —
  restore-point semantics are disk-equivalent, which is what redeploy restores anyway
- guest agent channel -> none on GCP; the boxes carry their identity in metadata and
  the driver reaches the engine/red01 through their (firewalled) public IPs. Every
  through-the-engine SSH path the phases use runs unchanged.
"""

import hashlib
import json
from pathlib import Path

from common import utils
from common.constants import RED_SEGMENT_GW, RED_SEGMENT_IP, RED_SEGMENT_SUBNET, \
    WINDOWS_ADMIN_USER, GOLDEN_TAG
from common.ssh_ops import forget_engine_host_key, ssh_via_gateway, wait_for_ssh
from common.timing import timed
from providers.gcp import compute, image_ops, instance_ops, network_ops, naming
from providers.gcp.naming import labels as ownership_labels

PLATFORM = "gcp"

# The windows bootstrap marker must hold this long before the box is called done:
# the script writes it immediately before a pending-features reboot, so one sighting
# can race the shutdown.
BOOTSTRAP_STABLE_S = 60

# The engine's and red01's management NICs live on the mgmt subnet (ops ingress).
MGMT_ENGINE_IP = network_ops.MGMT_ENGINE_IP
MGMT_RED01_IP = "10.224.0.20"
RED01_TAG = network_ops.RED01_TAG


def _creds():
    return compute.session()


def _prefix(ctx):
    return naming.prefix(ctx.comp_name, ctx.run_id)


def _ssh_public_key():
    from estate import config
    pub = Path(str(config.ssh_key_path()) + ".pub")
    return pub.read_text().strip() if pub.is_file() else ""


def _instance_name(prefix, target):
    return naming.instance_name(prefix, target["vm_name"])


# ---- phase 1 ----

def reclaim_interrupted(ctx):
    """This same run's crash leftovers: everything carrying our full ownership set that a
    failed attempt created and never recorded (instances only — an interrupted run's
    IMAGES carry the right hash stamps and are its resume-usable state, exactly like the
    proxmox kept-template reuse). Anything of another run is not touched here; the
    preflight already refused over it."""
    creds = _creds()
    ours = [i for i in compute.instances(creds)
            if naming.owned(i, ctx.comp_name, ctx.run_id)]
    if not ours:
        print("  Reclaim: nothing of this run left standing")
        return
    for info in ours:
        instance_ops.delete(creds, info["name"])
        print(f"  reclaiming {info['name']} (this run's interrupted attempt)")


# ---- phase 2: the engine template image ----

def engine_template(ctx):
    from engine.engine_bootstrap_ops import bootstrap_scoring_engine
    from engine.engine_identity_ops import clean_engine_for_template, stamp_engine_build
    from engine import template_freeze
    from estate import config
    from common.utils import compfile_value, stages_red
    from deploy_lib.phases._estate import has_in_path_firewall, slot_team_identifiers

    creds = _creds()
    prefix = _prefix(ctx)
    # The build VM lives on this run's OWN mgmt subnet (behind the same ingress rule the
    # engine gets), so the VPC must exist before the template does — create_estate later
    # re-runs ensure_network and finds it all standing.
    network_ops.ensure_network(ctx.document(), comp_name=ctx.comp_name, run_id=ctx.run_id,
                               team_identifiers=slot_team_identifiers(ctx),
                               in_path=has_in_path_firewall(ctx), red=stages_red(ctx.comp_dir))
    name = naming.image_name(prefix, "engine")
    base = image_ops.resolve_base(creds, "engine-base")
    quotient_ref = compfile_value(ctx.comp_dir / "Compfile", "quotient_ref")
    # The PVE hash key is the base template's vmid; gcp's base identity is the resolved
    # image URL, normalized to an int so the SAME freeze machinery drives drift.
    base_key = int.from_bytes(hashlib.sha256(base.encode()).digest()[:8], "big")
    engine_inputs = template_freeze.engine_hash_inputs(base_key, quotient_ref,
                                                        bootstrap_scoring_engine)
    engine_hash = template_freeze.hash_from_inputs(engine_inputs)
    stored = template_freeze.load_template_hashes(ctx.comp_dir)

    existing = compute.image(creds, name)
    rebuild = True
    if existing is not None:
        if image_ops.image_hash(existing) == image_ops.hash_value(engine_hash):
            print(f"  Engine image hash matches — reusing {name}")
            rebuild = False
        elif not template_freeze.frozen_gate(
                ctx.comp_dir, (stored.get("engine") or {}).get("inputs"),
                engine_inputs, "engine template"):
            print(f"  Engine image {name}: frozen — keeping the stamped image")
            rebuild = False
    if rebuild:
        if existing is not None:
            print("  Engine image hash differs — rebuilding...")
            image_ops.delete_image(creds, name)
        vm_name = f"{prefix}-enginebuild"
        instance_ops.delete(creds, vm_name)
        username = config.active().engine.username
        build = instance_ops.create(
            creds, vm_name, image=base,
            machine=instance_ops.machine_type(4, 8192),
            nics=[instance_ops.nic(naming.subnet_name(prefix, "mgmt"), MGMT_ENGINE_IP,
                                   external="EPHEMERAL")],
            labels=ownership_labels(ctx.comp_name, ctx.run_id, "engine-build"),
            disk_gb=40, network_tags=(network_ops.ENGINE_TAG,),
            meta=instance_ops.metadata(username, _ssh_public_key()))
        build_ip = (build["networkInterfaces"][0].get("accessConfigs") or [{}])[0].get("natIP")
        forget_engine_host_key(build_ip)
        print(f"    engine build VM {build_ip} (from {base})")
        if not wait_for_ssh(Path(ctx.ssh_key_abs), username, build_ip, timeout=900):
            raise RuntimeError(f"engine-template build VM {build_ip} never accepted SSH")
        build_ctx = {"ssh_key_path": str(ctx.ssh_key_abs), "vm_username": username,
                     "box_username": username, "scoring_engine_ip": build_ip,
                     "engine_build": {"name": vm_name}}
        stamp_engine_build(build_ctx, template_freeze.engine_template_vmid(ctx.engine_vmid))
        if not _box_settled(build_ctx, build_ip, vmid=ctx.engine_vmid):
            print("    WARNING: engine build VM never fully settled — proceeding")
            utils.record_degradation("engine build VM never fully settled", build_ip)
        bootstrap_scoring_engine(build_ctx, ctx.postgres_password, ctx.redis_password,
                                 quotient_ref=quotient_ref)
        clean_engine_for_template(build_ctx,
                                  template_freeze.engine_template_vmid(ctx.engine_vmid))
        instance_ops.stop(creds, vm_name)
        image_ops.create_from_instance(
            creds, name, compute.instance(creds, vm_name),
            labels={**ownership_labels(ctx.comp_name, ctx.run_id, "engine-template"),
                    image_ops.HASH_LABEL: engine_hash})
        instance_ops.delete(creds, vm_name)
        print(f"    {name} is the engine template image (hash stamped)")
    ctx.state["engine_template_vmid"] = template_freeze.engine_template_vmid(ctx.engine_vmid)
    ctx.state["engine_template_hash"] = engine_hash
    template_freeze.save_template_hashes(
        ctx.comp_dir, engine={"hash": engine_hash, "inputs": engine_inputs})
    ctx.save_state()
    document = ctx.document()
    document.record("engine_template", compute.zone(), name)
    document.save()
    return name


def _box_settled(build_ctx, ip, vmid):
    """cloud-init + apt quiet, over the SSH channel (there is no guest agent on gcp)."""
    from guests.box_settle_ops import _box_settled_via_ssh
    return bool(_box_settled_via_ssh(build_ctx, {"vmid": vmid, "ip": ip}))


# ---- phase 2: networks + the engine ----

def create_estate(ctx):
    from deploy_lib.phases._estate import has_in_path_firewall, slot_team_identifiers
    from common.utils import stages_red
    from estate import config

    creds = _creds()
    prefix = _prefix(ctx)
    teams = slot_team_identifiers(ctx)
    document = ctx.document()
    with timed(ctx.comp_dir, 2, "create_network"):
        net = network_ops.ensure_network(
            document, comp_name=ctx.comp_name, run_id=ctx.run_id,
            team_identifiers=teams, in_path=has_in_path_firewall(ctx),
            red=stages_red(ctx.comp_dir))
    engine_name = _instance_name(prefix, {"vm_name": "quotient-engine"})
    engine_image = naming.image_name(prefix, "engine")
    username = config.active().engine.username
    if compute.instance(creds, engine_name) is None:
        nics = [instance_ops.nic(net["mgmt_subnet"], MGMT_ENGINE_IP,
                                 external="EPHEMERAL")]
        # The VCP owns each subnet's .1 (the gateway); the engine holds .2 per team and
        # the range's default VPC ROUTE points at it, so it stays the router.
        for identifier in teams:
            nics.append(instance_ops.nic(naming.subnet_name(prefix, "t", identifier),
                                         utils.engine_team_gateway(identifier, PLATFORM)))
        if has_in_path_firewall(ctx):
            for identifier in teams:
                nics.append(instance_ops.nic(naming.subnet_name(prefix, "w", identifier),
                                             f"172.31.{identifier}.1"))
        if stages_red(ctx.comp_dir):
            nics.append(instance_ops.nic(naming.subnet_name(prefix, "red"), "10.200.0.2"))
        instance_ops.create(
            creds, engine_name,
            image=f"projects/{creds['project_id']}/global/images/{engine_image}",
            machine=instance_ops.machine_type(4, 8192), nics=nics,
            labels=ownership_labels(ctx.comp_name, ctx.run_id, "engine",
                                     unit=ctx.engine_vmid),
            disk_gb=40, network_tags=(network_ops.ENGINE_TAG,),
            can_ip_forward=True,   # the range's router: the VPC default route lands here
            meta=instance_ops.metadata(username, _ssh_public_key()))
        document.record("engine", compute.zone(), engine_name, vmid=ctx.engine_vmid)
        document.save()
    # Outside the create branch: an ADOPTED engine must still converge its route —
    # the via-engine default is team-tag-scoped now, and adopt-not-rescope left a
    # stale scope steering red01's egress into the un-MASQUERADE'd engine path.
    network_ops.ensure_engine_route(creds, prefix, engine_name, teams)
    # The engine's PUBLIC ip is the run's mgmt address (firewalled to ingress_sources)
    # and the address every later tool records as the scoring engine.
    primary = instance_ops.primary_nic_info(creds, engine_name)
    if not primary["public"]:
        raise RuntimeError("the engine's external IP is missing — the mgmt NIC create failed")
    ctx.engine_mgmt_ip = primary["public"]
    ctx.state["engine_mgmt_ip"] = primary["public"]
    document.section("identity")["engine_mgmt_ip"] = primary["public"]
    ctx.save_state()
    # The netplan pins NICs by MAC in create order (mgmt first); the netplan's index 0
    # is the FIRST TEAM NIC, so handler MACs start after NIC0, as on proxmox.
    ctx.engine_nic_macs = instance_ops.nic_macs(creds, engine_name)
    print(f"  Engine: {engine_name} at {primary['public']} (mgmt)")


# ---- phase 4: goldens ----

def build_golden_sets(ctx, stored, coverage_recorder):
    """Per box type: instance from the base image -> plant (the SSH machinery the
    proxmox flow uses) -> tz-base machine image -> custom image stamped with the
    golden hash, mirroring `qm template`. The image's OWN hash label decides reuse,
    the same node-side truth the PVE template description carries."""
    from common.constants import SNAP_BASE
    from common.ssh_ops import wait_for_boxes_ssh, wait_for_cloud_init
    from engine import template_freeze
    from golden import golden_target_ops
    from guests.apt_dns_ops import fix_dns_on_boxes, prep_apt_on_boxes
    from guests.ubuntu_auth_ops import setup_ubuntu_auth
    from nakonops.nakon_bundle_ops import build_nakon_bundle
    from nakonops.nakon_run_ops import run_nakon

    creds = _creds()
    prefix = _prefix(ctx)
    targets = golden_target_ops.golden_targets(ctx.engine_vmid, ctx.teams, ctx.boxes,
                                               slot=0, anchor_identifier=None)
    machine_cfg = json.loads(ctx.golden_config_path.read_text())["machines"]
    by_box = {m["name"].removesuffix("-golden"): m for m in machine_cfg}
    golden_ids = {}

    for target in targets:
        box = target["box"]["name"]
        image = naming.image_name(prefix, "golden", box)
        machine = _instance_name(prefix, target)
        digest = (ctx.golden_hashes or {}).get(box) or ""
        existing = compute.image(creds, image)
        if existing is not None:
            if image_ops.image_hash(existing) == image_ops.hash_value(digest):
                print(f"  Golden {box}: image hash matches — reusing {image}")
                golden_ids[box] = target["vmid"]
                continue
            if not template_freeze.frozen_gate(
                    ctx.comp_dir, (stored.get("golden") or {}).get(box, {}).get("inputs"),
                    (ctx.golden_inputs or {}).get(box), f"golden {box}"):
                print(f"  Golden {box}: frozen — keeping the stamped image {image}")
                golden_ids[box] = target["vmid"]
                continue
            print(f"  Golden {box}: hash differs — rebuilding {image}")
            image_ops.delete_image(creds, image)

        image_labels = ownership_labels(ctx.comp_name, ctx.run_id, GOLDEN_TAG,
                                        unit=target["vmid"])
        if digest:
            image_labels = {**image_labels, image_ops.HASH_LABEL: digest}
        if box in (ctx.unbooted or ()):
            # A domain controller's golden stays unbooted: the base cut straight into an
            # image, so each team's clone specializes its own SID and the pre-domain
            # stage promotes per team (proxmox's _build_unbooted_goldens, with the
            # image replacing clone+qm-template).
            image_ops.create_from_image(
                creds, image, image_ops.resolve_base(creds, target["box"]["template"]),
                image_labels)
            golden_ids[box] = target["vmid"]
            print(f"  Golden {box}: unbooted (dc) — image cut straight from the base")
            continue
        built = _build_one_golden(ctx, creds, prefix, target, machine, by_box, coverage_recorder)
        image_ops.create_from_instance(creds, image, built, labels=image_labels)
        instance_ops.delete(creds, machine)
        golden_ids[box] = target["vmid"]
        print(f"  Golden {box}: image {image} ready")
    return golden_ids, {0: golden_ids}


def _build_one_golden(ctx, creds, prefix, target, machine, by_box, coverage_recorder):
    from common.constants import SNAP_BASE
    from common.ssh_ops import wait_for_boxes_ssh, wait_for_cloud_init
    from common.windows_ops import is_windows_template
    from guests.apt_dns_ops import fix_dns_on_boxes, prep_apt_on_boxes
    from guests.ubuntu_auth_ops import setup_ubuntu_auth
    from golden.golden_ops import windows_plan_features
    from nakonops.nakon_bundle_ops import build_nakon_bundle
    from nakonops.nakon_run_ops import run_nakon

    box = target["box"]["name"]
    identifier = target["ip"].split(".")[2]  # goldens sit on team1's subnet at .240+
    windows = is_windows_template(target["box"]["template"])
    info = compute.instance(creds, machine)
    if info is None:
        base = image_ops.resolve_base(creds, target["box"]["template"])
        # Windows boxes boot with no cloud-init: the SAME startup script the team boxes
        # get bakes the full proxmox bootstrap contract (admin password, defender off,
        # RDP, plan features) into the golden disk, so every clone inherits it complete.
        meta = instance_ops.metadata(
            ctx.box_username, _ssh_public_key(),
            image_ops.cloud_config_user(ctx.box_username, ctx.box_password,
                                        _ssh_public_key()),
            windows_startup_ps1=windows_startup_script(
                windows_plan_features(ctx.comp_dir, box), ctx.box_password) if windows else "")
        info = instance_ops.create(
            creds, machine, image=base,
            machine=instance_ops.machine_type(target["box"]["cpu"],
                                               target["box"]["memory_mb"]),
            nics=[instance_ops.nic(naming.subnet_name(prefix, "t", identifier),
                                   target["ip"])],
            labels=ownership_labels(ctx.comp_name, ctx.run_id, "golden-build",
                                    unit=target["vmid"]),
            meta=meta,
            network_tags=(naming.team_tag(identifier),))
        print(f"  Golden {box}: instance {machine} from {base}")
    else:
        print(f"  Golden {box}: adopting the staged instance {machine} (re-planting)")
        instance_ops.start(creds, machine)
    # Windows goldens spend 5-15 min in sysprep specialize before first logon, and
    # sshd's capability install lands only after that; 1800 s is the honest ceiling.
    wait_for_boxes_ssh(ctx.tf_ctx, [target], timeout=1800)
    if windows:
        # The marker wait also covers the script's pending-features reboot: the box
        # must come back up and HOLD the marker before the plant dials it.
        windows_bootstrap(ctx, target)
    else:
        wait_for_cloud_init(ctx.tf_ctx, [target], timeout=240)
        setup_ubuntu_auth([target], ctx.tf_ctx)
        fix_dns_on_boxes([target], ctx.tf_ctx)
        prep_apt_on_boxes([target], ctx.tf_ctx)
    image_ops.machine_image_mark(
        creds, naming.machine_image_name(prefix, target["vm_name"], SNAP_BASE),
        machine, ownership_labels(ctx.comp_name, ctx.run_id))

    machines = [{"name": f"{box}-golden",
                 "configurations": (by_box.get(box) or {}).get("configurations", [])}]
    result = None
    if machines[0]["configurations"]:
        bundle = build_nakon_bundle(ctx.golden_config_path)
        result = run_nakon(ctx.ssh_key, ctx.scoring_user, ctx.scoring_ip, bundle,
                          ctx.golden_config_path, only=[f"{box}-golden"],
                          timeout=2400, strict=True, jobs=ctx.nakon_jobs,
                          run_tag="golden")
        if result.failed:
            raise RuntimeError(f"golden plant for {box} had {len(result.failed)} FAILED "
                              f"step(s) — strict mode requires green")
    coverage_recorder(machines, result)
    if not windows:
        # Clones must re-run cloud-init as themselves; a failed clean ships the golden's
        # machine-id (and first-boot state) to every clone. The proxmox golden flow
        # checks the same rc before converting.
        clean = ssh_via_gateway(ctx.tf_ctx, target["ip"],
                                "sudo cloud-init clean --logs --machine-id", timeout=120)
        if clean.returncode != 0:
            raise RuntimeError(f"golden {box}: cloud-init clean failed rc={clean.returncode} "
                               f"{(clean.stderr or '').strip()[:120]}")
    instance_ops.stop(creds, machine)
    return compute.instance(creds, machine)


def apply_team_boxes(ctx, sources):
    creds = _creds()
    prefix = _prefix(ctx)
    from common.utils import is_unmanaged
    from common.windows_ops import is_windows_template
    from golden.golden_ops import windows_plan_features
    for target in ctx.all_targets:
        box = target["box"]
        if is_unmanaged(box):
            # in-path appliances are refused at preflight's matrix check; reaching here
            # means a lineup dodged it — refuse rather than half-deploy a dead box.
            raise SystemExit(f"  ERROR: unmanaged box {box['name']} has no gcp path — the "
                             f"platform matrix refuses this lineup")
        name = _instance_name(prefix, target)
        if compute.instance(creds, name) is not None:
            continue
        golden_image = naming.image_name(prefix, "golden", box["name"])
        if is_windows_template(box["template"]):
            meta = instance_ops.metadata(
                ctx.box_username, _ssh_public_key(),
                windows_startup_ps1=windows_startup_script(
                    windows_plan_features(ctx.comp_dir, box["name"]),
                    ctx.box_password))
        else:
            meta = instance_ops.metadata(ctx.box_username, _ssh_public_key())
        instance_ops.create(
            creds, name,
            image=f"projects/{creds['project_id']}/global/images/{golden_image}",
            machine=instance_ops.machine_type(box["cpu"], box["memory_mb"]),
            nics=[instance_ops.nic(naming.subnet_name(prefix, "t", target["identifier"]),
                                   target["ip"])],
            labels=ownership_labels(ctx.comp_name, ctx.run_id, "team_box",
                                    unit=target["vmid"]),
            network_tags=(naming.team_tag(target["identifier"]),),
            disk_gb=box.get("disk_gb") or 10,
            meta=meta)
        ctx.document().record("team_box", compute.zone(), name, vmid=target["vmid"])
    ctx.document().save()


def windows_startup_script(features, admin_password):
    # The gcp Windows boot channel: the GCE guest environment runs
    # windows-startup-script-ps1 as SYSTEM at EVERY boot, so the script must be
    # idempotent and marker-guarded the way a resume is. The body mirrors the proxmox
    # bootstrap_windows_box contract (defender off, RDP on, firewall profiles on,
    # plan features) minus the transport-specific pieces (static IP re-assign, QEMU-GA).
    feature_block = ""
    reboot_line = ""
    if features:
        feature_list = ", ".join(f"'{f}'" for f in features)
        # A feature install can hit ERROR_SHARING_VIOLATION (0x80070020) while setup
        # holds the file; that is transient, so retry it (the proxmox block, verbatim).
        feature_block = f"""
$featureOk = $false
for ($attempt = 0; $attempt -lt 3 -and -not $featureOk; $attempt++) {{
  try {{
    $installed = Install-WindowsFeature -Name @({feature_list}) -IncludeManagementTools -ErrorAction Stop
    $featureOk = $true
    if ($installed.RestartNeeded -eq 'Yes') {{ $reboot = $true }}
  }} catch {{
    if ($_.Exception.Message -notmatch '0x80070020|being used by another process') {{ throw }}
    Start-Sleep -Seconds 20
  }}
}}
if (-not $featureOk) {{ throw 'Install-WindowsFeature failed after 3 attempts' }}"""
        # Features stay InstallPending until a real restart: the marker is WITHHELD and
        # the box reboots; the next boot's script finds nothing pending and writes it.
        reboot_line = "\nif ($reboot) { Restart-Computer -Force }"
    return f"""
$ErrorActionPreference = 'Stop'
$marker = 'C:\\ProgramData\\tezcatlipoca\\bootstrap.done'
try {{
  $sshKeys = Invoke-RestMethod -Headers @{{ MetadataFlavor = 'Google' }} -Uri 'http://metadata.google.internal/computeMetadata/v1/instance/attributes/ssh-keys'
  $sshDir = 'C:\\\\ProgramData\\\\ssh'
  New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
  $authorized = Join-Path $sshDir 'administrators_authorized_keys'
  If (-not (Test-Path $authorized)) {{ New-Item -ItemType File -Path $authorized | Out-Null }}
  foreach ($line in ($sshKeys -split "`n" | Where-Object {{ $_.Trim() }})) {{
    $u, $keyType = ($line -split ':', 2)
    $u = $u.Trim()
    If (-not (Get-LocalUser -Name $u -ErrorAction SilentlyContinue)) {{
      net user $u '{admin_password}' /add /y | Out-Null
    }} Else {{
      net user $u '{admin_password}' /y | Out-Null
    }}
    net localgroup Administrators $u /add | Out-Null
    $have = @(Get-Content $authorized -ErrorAction SilentlyContinue)
    $key = ($line -split ':', 2)[1].Trim()
    If (($have | Where-Object {{ $_  -eq $key }}).Count -eq 0) {{ Add-Content -Path $authorized -Value $key }}
  }}
  icacls $authorized /inheritance:r /grant 'SYSTEM:F' /grant 'BUILTIN\\\\Administrators:F' | Out-Null
  Get-Service sshd -ErrorAction SilentlyContinue | Restart-Service -Force
}} catch {{
  Write-Output "ssh-key sync failed: $($_.Exception.Message)"
}}
net user {WINDOWS_ADMIN_USER} "{admin_password}" /y /active:yes
if ($LASTEXITCODE -ne 0) {{ throw "net user failed rc=$LASTEXITCODE" }}
try {{ Uninstall-WindowsFeature -Name Windows-Defender -ErrorAction SilentlyContinue | Out-Null }} catch {{}}
# The uninstall alone is not the competition standard: the realtime scanners
# quarantine red's payloads on sight even while the uninstall is pending a restart.
if (Get-Command Set-MpPreference -ErrorAction SilentlyContinue) {{
    Set-MpPreference -DisableRealtimeMonitoring $true -ErrorAction SilentlyContinue
    Set-MpPreference -DisableIOAVProtection $true -ErrorAction SilentlyContinue
    Set-MpPreference -DisableScriptScanning $true -ErrorAction SilentlyContinue
}}
sc.exe config WinDefend start= disabled | Out-Null
sc.exe stop WinDefend | Out-Null
New-Item -Path 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows Defender' -Force -ErrorAction SilentlyContinue | Out-Null
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows Defender' -Name DisableAntiSpyware -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows Defender' -Name DisableAntiVirus -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
{feature_block}
$cap = Get-WindowsCapability -Online | Where-Object {{ $_.Name -like 'OpenSSH.Server*' }}
if ($cap -and $cap.State -ne 'Installed') {{
    Add-WindowsCapability -Online -Name $cap.Name | Out-Null
}}
Set-Service -Name sshd -StartupType Automatic
Start-Service -Name sshd
# The stock image ships all three firewall profiles disabled: until they are on, the
# sshd rule below (and any later blue hardening) is dead paper. Idempotent.
try {{ Set-NetFirewallProfile -All -Enabled True -ErrorAction SilentlyContinue | Out-Null }} catch {{}}
if (-not (Get-NetFirewallRule -Name sshd -ErrorAction SilentlyContinue)) {{
    New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}}
# Every CCDC blue team administers Windows over RDP: the scored 3389 dial is filtered
# without both the terminal-server setting and the rule group (proxmox contract).
Set-ItemProperty 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server' -Name fDenyTSConnections -Value 0 -ErrorAction SilentlyContinue
Set-Service -Name TermService -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service -Name TermService -ErrorAction SilentlyContinue
Enable-NetFirewallRule -Name RemoteDesktop-UserMode-In-TCP,RemoteDesktop-UserMode-In-UDP,RemoteDesktop-Shadow-In-TCP -ErrorAction SilentlyContinue
New-Item $marker -Force | Out-Null{reboot_line}
"""


def windows_bootstrap(ctx, target, features=(), timeout=1800):
    """Windows boxes self-bootstrap at first boot from windows-startup-script-ps1,
    baked into the instance metadata at box create (the plan features ride the same
    metadata). This wait verifies the channel came up: sshd answering through the
    engine gateway and the bootstrap marker written. The marker only proves the
    script finished with nothing pending a restart, but its first sighting can race
    the pending-features reboot's shutdown — so it must survive one poll cycle."""
    import time
    from common.ssh_ops import ssh_via_gateway
    deadline = time.time() + timeout
    ip = target["ip"]
    marker_cmd = ('powershell -NoProfile -Command "exit [int](-not (Test-Path '
                  "'C:\\ProgramData\\tezcatlipoca\\bootstrap.done'))\"")
    stable_since = None
    while True:
        ok = ssh_via_gateway(ctx.tf_ctx, ip, marker_cmd, timeout=30,
                             user=ctx.tf_ctx.get("box_username", "ubuntu")).returncode == 0
        if ok:
            now = time.time()
            if stable_since and now - stable_since >= BOOTSTRAP_STABLE_S:
                return {"ok": True, "vmid": target.get("vmid"), "ip": ip,
                        "channel": "windows startup script + sshd"}
            stable_since = stable_since or now
        else:
            stable_since = None
        if time.time() >= deadline:
            raise RuntimeError(f"vmid {target.get('vmid')}: the windows bootstrap never "
                               f"settled on {ip} within {timeout}s (sshd + bootstrap.done marker)")
        time.sleep(10)


# ---- restore points (machine images) ----

def restore_point_create(ctx, target, name, description=""):
    creds = _creds()
    prefix = _prefix(ctx)
    instance = _instance_name(prefix, target)
    info = compute.instance(creds, instance)
    if info is None:
        print(f"    WARNING: snapshot {name} skipped — {instance} does not exist")
        return False
    machine_image = naming.machine_image_name(prefix, target["vm_name"], name)
    was_running = info.get("status") != "TERMINATED"
    if was_running:
        instance_ops.stop(creds, instance, timeout=1800)
    try:
        image_ops.machine_image_mark(
            creds, machine_image, instance,
            ownership_labels(ctx.comp_name, ctx.run_id, unit=target["vmid"]))
    finally:
        if was_running:
            instance_ops.start(creds, instance)
    return True


def restore_points_list(ctx, target):
    creds = _creds()
    prefix = _prefix(ctx)
    head = f"{prefix}-{target['vm_name']}-"
    marks = set()
    for name in image_ops.list_marks(creds, ownership_labels(ctx.comp_name, ctx.run_id)):
        if name.startswith(head):
            marks.add(name[len(head):])
    return marks


def restore_point_rollback(ctx, target, name):
    raise SystemExit(
        "  ERROR: redeploy's rollback modes are not implemented on gcp yet — a machine "
        "image restores by RECREATING the instance, which the redeploy engine does not "
        "do today. Use --mode reconfigure, or redeploy the range. Tracked as the gcp "
        "provider's redeploy workstream.")


def restore_point_hint(ctx, target):
    if not restore_points_list(ctx, target):
        return (f"no machine images for {target['vm_name']} — either this range predates "
                f"snapshotting or the machine-images quota filled. Use --mode "
                f"reconfigure on gcp.")
    return ""


# ---- resume guards ----

def live_unit_numbers():
    """Every machine's unit label in this zone: guard_resume_existence asks whether the
    roster's recorded units exist, and an instance's tz-unit label is its live proof."""
    creds = compute.session()
    units = set()
    for info in compute.instances(creds):
        raw = (info.get("labels") or {}).get(naming.LABEL_UNIT)
        if raw:
            try:
                units.add(int(raw))
            except ValueError:
                continue
    return units


# ---- recorded-estate teardown ----

def destroy_estate(document, *, full=True, nodes=None, log=print):
    creds = compute.session()
    identity = document.data.get("identity") or {}
    comp = identity.get("comp_name") or ""
    run_id = identity.get("run_id") or ""
    prefix = naming.prefix(comp, run_id)
    destroyed, refused, skipped, kept = [], [], [], []

    for kind, role in (("team_box", None), ("jump", None), ("engine", None),
                       ("golden", GOLDEN_TAG), ("engine_template", "engine-template")):
        for row in document.resources(kind):
            name = row["name"]
            if kind in ("golden", "engine_template") and not full:
                kept.append(dict(row))
                log(f"  kept {kind} {name} (--teams-only: templates stay)")
                continue
            if kind in ("golden", "engine_template"):
                info = compute.image(creds, name)
                if info is None:
                    document.forget(kind, name=name)
                    continue
                if not naming.owned(info, comp, run_id, role or naming._ALL):
                    refused.append({"entry": dict(row),
                                    "reason": f"{name} is not this run's "
                                              f"(labels {info.get('labels')})"})
                    continue
                image_ops.delete_image(creds, name)
            else:
                info = compute.instance(creds, name)
                if info is None:
                    document.forget(kind, name=name)
                    continue
                if not naming.owned(info, comp, run_id):
                    refused.append({"entry": dict(row),
                                    "reason": f"{name} is not this run's "
                                              f"(labels {info.get('labels')})"})
                    continue
                instance_ops.delete(creds, name)
            document.forget(kind, name=name)
            destroyed.append(dict(row))
            log(f"  destroyed {kind} {name}")

    # Drain unrecorded labeled instances BEFORE the subnet deletes: an instance the
    # inventory never recorded still pins its subnet, and the API refuses the subnet
    # (live: net-t101 held by an unrecorded team box — every teardown pass died at
    # the network delete before the machinery's after-the-fact sweep ever ran).
    for info in [i for i in compute.instances(creds)
                 if naming.owned(i, comp, run_id)]:
        if not full and (info.get("labels") or {}).get(naming.LABEL_ROLE):
            continue
        instance_ops.delete(creds, info["name"])
        log(f"  swept instance {info['name']} (label net)")

    # restore points first-refusal-free: machine images can only be this run's names
    for mark in [m for m in image_ops.list_marks(creds, ownership_labels(comp, run_id))]:
        image_ops.delete_machine_image(creds, mark)
        log(f"  destroyed machine image {mark}")

    if full:
        for row in document.resources("network"):
            if network_ops.delete_network(creds, row["name"], log=log):
                destroyed.append(dict(row))
                document.forget("network", name=row["name"])
        for row in document.resources("subnet"):
            document.forget("subnet", name=row["name"])
    document.save()
    return {"destroyed": destroyed, "skipped": skipped, "refused": refused, "kept": kept}


def sweep_tagged_leftovers(nodes, ownership, keep_templates=False):
    """The label-scope net: instances carrying the FULL ownership set a crash left
    unrecorded. A kept template's role label stays out of this sweep."""
    creds = compute.session()
    ours = [i for i in compute.instances(creds)
            if naming.owned(i, ownership.comp, ownership.run_id)]
    if not ours:
        return []
    print(f"  Leftover sweep: {len(ours)} instance(s) carrying this run's labels")
    for info in ours:
        if keep_templates and (info.get("labels") or {}).get(naming.LABEL_ROLE):
            continue
        instance_ops.delete(creds, info["name"])
        print(f"    swept {info['name']}")
    return []


def pre_stop_boxes(teams, boxes, default_node, team_nodes=None, ownership=None):
    # The delete stops the instance itself; the PVE ACPI-hang hazard has no analog.
    return None


# ---- red01 ----

def red01_deploy(comp_dir, **kwargs):
    from red import red01_ops
    from red.tlaloc_ops import red_disk_gb
    from estate import config

    own = kwargs.get("ownership")
    if own is None:
        raise RuntimeError("red01_deploy needs the run's Ownership")
    creds = _creds()
    comp = utils.competition_name(comp_dir)
    prefix = naming.prefix(comp, own.run_id)
    unit = red01_ops.red01_vmid_for(comp_dir, kwargs.get("engine_vmid"),
                                    kwargs.get("placement"))
    name = naming.instance_name(prefix, "tlaloc-red01")
    info = compute.instance(creds, name)
    if info is not None:
        if not naming.owned(info, comp, own.run_id):
            raise RuntimeError(f"red01 slot {name} is occupied by a machine that is not "
                               f"this run's (labels {info.get('labels')}) — a deploy never "
                               f"reconfigures or destroys another run's range")
        print(f"  red01      : adopting {name} (this run's)")
    else:
        pub = _pub_of(kwargs.get("ssh_key"))
        instance_ops.create(
            creds, name,
            image=image_ops.resolve_base(creds, "red01-base"),
            machine=instance_ops.machine_type(4, 8192),
            nics=[instance_ops.nic(naming.subnet_name(prefix, "mgmt"), MGMT_RED01_IP,
                                   external="EPHEMERAL"),
                  instance_ops.nic(naming.subnet_name(prefix, "red"), RED_SEGMENT_IP)],
            labels=ownership_labels(comp, own.run_id, "red01", unit=unit),
            disk_gb=int(red_disk_gb(comp_dir) or 60),
            network_tags=(RED01_TAG,),
            meta=instance_ops.metadata(red01_ops.RED_USER, pub,
                                       _red01_user_data(pub)))
        print(f"  red01      : instance {name} (mgmt via public NAT, red {RED_SEGMENT_IP})")
    primary = instance_ops.primary_nic_info(creds, name)
    if not primary["public"]:
        raise RuntimeError("red01's external IP is missing — its mgmt NIC create failed")
    red01_ops.record_identity(comp_dir, kwargs.get("placement"), vmid=unit,
                              ip=primary["public"], node=compute.zone())
    return {"ok": True, "vmid": unit, "ip": primary["public"], "node": compute.zone(),
            "segment_ip": RED_SEGMENT_IP, "segment_subnet": RED_SEGMENT_SUBNET}


def _pub_of(key):
    pub = Path(str(key) + ".pub")
    if not pub.is_file():
        raise RuntimeError(f"the SSH public key {pub} is missing — red01 cannot be built "
                           f"without the key that reaches it")
    return pub.read_text().strip()


def _red01_user_data(pub):
    # Identity + the red-segment route pair: DHCP gives red01 no route for the team
    # or transit nets (they are not its subnets), so team-facing dials fell to the
    # mgmt default and left with src 10.224.0.20 — fw-red-to-t<id> admits 10.200.0.0/24
    # only, and the day-0 seed died on every box (live gcp-acc19: `ip route get` picked
    # ens4; proxmox's red01 carries the same route pair in its netplan).
    return (
        "#cloud-config\n"
        "users:\n"
        "  - name: sysadmin\n"
        "    sudo: ALL:(ALL) NOPASSWD:ALL\n"
        "    shell: /bin/bash\n"
        "    ssh_authorized_keys:\n"
        f"      - {pub}\n"
        "write_files:\n"
        "  - path: /etc/netplan/70-red-segment-routes.yaml\n"
        "    permissions: '0600'\n"
        "    content: |\n"
        "      network:\n"
        "        version: 2\n"
        "        ethernets:\n"
        # GCE NIC order: ens4 = mgmt, ens5 = red
        "          ens5:\n"
        "            routes:\n"
        "              - to: 192.168.0.0/16\n"
        f"                via: {RED_SEGMENT_GW}\n"
        "              - to: 172.31.0.0/16\n"
        f"                via: {RED_SEGMENT_GW}\n"
        # tlaloc is staged, never pip-installed — proxmox's red01 template bakes its
        # deps; a stock GCE image bakes nothing, and the seed died importing paramiko
        # (live gcp-acc20: 'ssh admin@192.168.101.10: No module named paramiko').
        "runcmd:\n"
        "  - [netplan, apply]\n"
        "  - [apt-get, update]\n"
        "  - [apt-get, install, -y, python3-paramiko, python3-requests]\n"
    )


def red01_destroy(comp_dir, **kwargs):
    from red import red01_ops
    from estate import state as estate_state

    own = kwargs.get("ownership")
    creds = _creds()
    comp = utils.competition_name(comp_dir)
    removed_vm = False
    if own:
        mine = [i for i in compute.instances(creds)
                if i["name"].endswith("tlaloc-red01")
                and naming.owned(i, comp, own.run_id)]
        if len(mine) > 1:
            raise RuntimeError("more than one red01 carries this run's labels — refusing")
        if mine:
            instance_ops.delete(creds, mine[0]["name"])
            print(f"  red01      : destroyed {mine[0]['name']}")
            removed_vm = True
    removed = {"vm": removed_vm, "rules": False}
    if kwargs.get("engine_ip") and kwargs.get("ssh_key") and kwargs.get("engine_user"):
        removed["rules"] = red01_ops.remove_engine_rules(
            kwargs["engine_ip"], kwargs["ssh_key"], engine_user=kwargs["engine_user"]
        ).get("ok", False)
    document = estate_state.EstateState.load(comp_dir)
    document.data.pop("red", None)
    document.save()
    return removed


def red01_occupant_verdict(vmid, comp, run_id=None):
    creds = _creds()
    mine = [i for i in compute.instances(creds)
            if i["name"].endswith("tlaloc-red01") and naming.owned(i, comp, run_id)]
    return "ours" if mine else "absent"
