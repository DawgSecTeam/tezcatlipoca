"""The provider registry: the estate backends a deploy can target.

`proxmox` and `gcp` are the two platforms; one range runs entirely on one of them
(per-run choice, recorded in the state document's identity section). The deploy
pipeline's provider-tied steps dispatch through `provider_platform(ctx)` ->
`implementation(platform)`, so a phase names an operation and the provider module
that carries it is resolved from the run's own identity — requesters never pick
an implementation the driver did not record.

Each provider package implements the same pipeline surface (`pipeline.py`):
PLATFORM, reclaim_interrupted, ensure_engine_template, create_engine_and_bridges,
build_goldens, create_team_boxes, bootstrap_windows_box, take_snapshot, and the
snapshot/query helpers redeploy needs (list_snapshot_names, rollback_snapshot,
snapshot_support_hint, live_units, preflight hooks and teardown).
"""

import importlib
import os

PROXMOX = "proxmox"
GCP = "gcp"
KNOWN = (PROXMOX, GCP)


def resolve(platform):
    if platform not in KNOWN:
        raise SystemExit(f"  ERROR: unknown provider {platform!r} — known: {', '.join(KNOWN)}")
    module = importlib.import_module(f"providers.{platform}.pipeline")
    if module.PLATFORM != platform:
        raise SystemExit(f"  ERROR: providers.{platform}.pipeline declares PLATFORM={module.PLATFORM!r}")
    return module


def platform_from_env(override=None, previous_state=None):
    """Flag, then TEZ_PLATFORM, then the state document's recorded identity, then proxmox."""
    if override:
        return override
    env = os.environ.get("TEZ_PLATFORM", "").strip()
    if env:
        return env
    recorded = (previous_state or {}).get("platform") or ""
    if recorded in KNOWN:
        return recorded
    return PROXMOX


def platform_of_ctx(ctx):
    platform = getattr(ctx, "platform", "") or ""
    if platform in KNOWN:
        return platform
    recorded = (getattr(ctx, "previous_state", None) or {}).get("platform") or ""
    if recorded in KNOWN:
        return recorded
    return PROXMOX


def of_ctx(ctx):
    return resolve(platform_of_ctx(ctx))


def platform_from_state(comp_dir):
    """The platform a recorded range ran on; a document without one is proxmox (pre-split)."""
    from estate import state
    flat = state.load_flat(comp_dir)
    recorded = flat.get("platform") if flat else ""
    return recorded if recorded in KNOWN else PROXMOX


def endpoint_label(platform):
    """The identity-section endpoint a deploy records for this platform (stale-state key)."""
    if platform == GCP:
        from estate import config
        from providers.gcp.client import load_credentials
        project = load_credentials(config.gcp_credentials_path())["project_id"]
        zone = config.active().gcp.zone
        # the state write normalizes a trailing slash away — the derived label must be
        # byte-identical to the recorded one or every gcp teardown self-refuses
        return f"gcp://{project}/{zone}" if zone else f"gcp://{project}"
    return ""


def require_identity(platform):
    """The lifecycle CLIs' gate: require the identity the RECORDED platform needs."""
    from estate import config
    if platform == GCP:
        config.require_gcp_identity()
    else:
        config.require_proxmox_identity()
