"""GCP preflight: every refusal that runs before any cloud mutation.

Order mirrors the proxmox gates: the platform matrix (lineups gcp cannot host yet
refuse at submit time, not mid-deploy), image-map resolution for every box type and
both engine/red01 bases, the leftover-range label sweep, the engine NIC ceiling,
quota headroom, and the headscale contract.
"""

from providers.gcp import compute
from providers.gcp.naming import LABEL_COMP, LABEL_RUN
from providers.gcp.client import gapi


def run_capacity_preflight(ctx, comp_dir, from_phase):
    if from_phase <= 2:
        creds = compute.session()
        _matrix_gate(ctx)
        _image_map_gate(creds, ctx)
        _leftover_gate(creds, ctx)
        _nic_gate(ctx)
        _quota_gate(creds, ctx)
        _headscale_gate(ctx)


def _matrix_gate(ctx):
    from common.windows_ops import is_windows_template
    problems = []
    for box in ctx.boxes:
        if box.get("in_path"):
            problems.append(
                f"{box['name']}: in-path firewalls need the appliance image tier, which "
                f"gcp does not have yet (VyOS-on-GCP is the pending work; pfSense cannot "
                f"run on GCP at all)")
        if box.get("unmanaged"):
            problems.append(f"{box['name']}: unmanaged appliance boxes have no gcp path")
        if is_windows_template(box["template"]):
            problems.append(
                f"{box['name']}: Windows boxes need the gcp boot-time channel "
                f"(startup-script-ps1 on stock GCE images), not implemented yet")
    if problems:
        raise SystemExit(
            "  ERROR: this lineup is not deployable on the gcp platform:\n      - "
            + "\n      - ".join(problems)
            + "\n      Deploy it on proxmox, or trim the boxes. The matrix grows as the "
              "gcp workstreams land.")


def _image_map_gate(creds, ctx):
    from providers.gcp import image_ops
    wanted = {box["template"] for box in ctx.boxes} | {"engine-base", "red01-base"}
    missing = []
    for template in sorted(wanted):
        try:
            image_ops.resolve_base(creds, template)
        except SystemExit as e:
            missing.append(str(e).strip())
    if missing:
        raise SystemExit("\n".join(missing))


def _leftover_gate(creds, ctx):
    """A range of this competition already standing is a refusal, never reaped — the
    same rule as a comp-tagged VM on PVE. Anything carrying tz-comp=<slug> refuses."""
    from common.constants import comp_slug
    slug = comp_slug(ctx.comp_name)
    leftovers = []
    for info in compute.instances(creds, [(LABEL_COMP, slug)]):
        run = (info.get("labels") or {}).get(LABEL_RUN) or "?"
        leftovers.append(f"instance {info['name']} (run {run})")
    for info in (gapi(creds, "GET", "global/images",
                     params={"filter": f"labels.{LABEL_COMP}={slug}",
                             "maxResults": 500}).get("items") or []):
        run = (info.get("labels") or {}).get(LABEL_RUN) or "?"
        leftovers.append(f"image {info['name']} (run {run})")
    if leftovers:
        raise SystemExit(
            f"  ERROR: this competition already has a gcp range standing:\n      - "
            + "\n      - ".join(leftovers)
            + "\n      A deploy never destroys a leftover range. Reclaim it with "
              f"destroy-competition.py (--comp-dir {ctx.comp_dir}), or resume the run "
              f"that owns it.")


def _nic_gate(ctx):
    from deploy_lib.phases._estate import has_in_path_firewall, slot_team_identifiers
    from common.utils import stages_red
    from estate import config
    teams = slot_team_identifiers(ctx)
    nics = 1 + len(teams) + (len(teams) if has_in_path_firewall(ctx) else 0) \
        + (1 if stages_red(ctx.comp_dir) else 0)
    # The machineTypes API does not expose per-family NIC limits, so the configured
    # ceiling is what preflight trusts; the create call refuses past the real one.
    ceiling = int(config.active().get("gcp.max_engine_nics") or 8)
    if nics > ceiling:
        raise SystemExit(
            f"  ERROR: the engine needs {nics} network interfaces (mgmt + {len(teams)} "
            f"teams + red), but the gcp ceiling is {ceiling} (gcp.max_engine_nics). "
            f"Raise the config value for a NIC-dense family, or shrink the range.")
    return nics


def _quota_gate(creds, ctx):
    from common.utils import stages_red
    region = compute.region()
    region_quota = {q["metric"]: q for q in gapi(creds, "GET", f"regions/{region}")["quotas"]}
    global_quota = {q["metric"]: q for q in gapi(creds, "GET", "")["quotas"]}

    teams = len(ctx.teams)
    goldens = len({box["template"] for box in ctx.boxes})
    cpus = sum(box["cpu"] for box in ctx.boxes) * teams \
        + (goldens + 2 + (1 if stages_red(ctx.comp_dir) else 0)) * 4
    disk_gb = sum(box.get("disk_gb") or 10 for box in ctx.boxes) * teams \
        + sum(box.get("disk_gb") or 10 for box in ctx.boxes) + 40 * (goldens + 2)
    machine_images = (2 * len(ctx.all_targets)          # tz-base + tz-ready per box
                      + goldens                          # golden build pre-plant marks
                      + 2)                               # seed slack
    checks = (
        (region_quota, "CPUS", cpus),
        (region_quota, "DISKS_TOTAL_GB", disk_gb),
        (region_quota, "IN_USE_ADDRESSES", 2),          # engine + red01
        (global_quota, "NETWORKS", 1),                   # the per-run VPC
        (global_quota, "MACHINE_IMAGES", machine_images),
        (global_quota, "IMAGES", goldens + 2),           # goldens + engine template + slack
    )
    problems = []
    for table, metric, needed in checks:
        entry = table.get(metric)
        if not entry:
            continue
        free = entry["limit"] - entry["usage"]
        if needed > free:
            problems.append(f"{metric}: this range needs ~{needed}, the project has "
                            f"{free:.0f} free (limit {entry['limit']:.0f}, usage "
                            f"{entry['usage']:.0f})")
    if problems:
        raise SystemExit(
            "  ERROR: the gcp project's quotas cannot hold this range:\n      - "
            + "\n      - ".join(problems)
            + "\n      Raise the quota in the GCP console, shrink the range (--teams), "
              "or reclaim ranges you no longer need.")
    print("  Preflight: gcp quotas ok")


def _headscale_gate(ctx):
    """Same contract as the proxmox gate: remote access ships with every deploy."""
    from common.utils import compfile_flag
    import subprocess
    if not compfile_flag(ctx.comp_dir / "Compfile", "remote_access", 1):
        return
    from net import remote_access_ops
    remote_access_ops._headscale_env()  # SystemExit naming the missing vars
    try:
        reachable = remote_access_ops.hs_cli("health", timeout=30,
                                             check=False).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        reachable = False
    if not reachable:
        raise SystemExit(
            "  ERROR: headscale is unreachable — remote access ships with every deploy, "
            "so the headscale host must be reachable before the gcp preflight passes. "
            "Fix TEZ_HEADSCALE_* in .env, or set `remote_access 0` in the Compfile.")
