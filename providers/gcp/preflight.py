"""GCP preflight: the deploy refusals that run before any cloud mutation.

Filled by the GCP backend (WS3): billing/API availability, quota headroom, a label
sweep for this competition's/leftover ranges, engine NIC ceiling vs team count,
image-map resolution for every box type, the in-path platform matrix, and the
red01 name slot. Until then a gcp run refuses here by name rather than
half-deploying into an unverified estate.
"""


def run_capacity_preflight(ctx, comp_dir, from_phase):
    if from_phase <= 2:
        raise SystemExit(
            "  ERROR: the gcp provider's capacity preflight is not implemented yet — "
            "no GCP deploy runs until it lands (quota/label/zone gates). Proxmox ranges "
            "are unaffected; deploy there, or wait for the gcp backend branch.")
