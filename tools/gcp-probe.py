"""Read-only probe of a GCP deploy target: identity, billing, APIs, quotas, NIC
ceilings, stock images. Creates nothing; safe to run any time.

Usage: python3 tools/gcp-probe.py [--credentials .gcp-sa.json] [--region us-central1]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from providers.gcp.client import COMPUTE_ROOT, gapi, load_credentials  # noqa: E402

PUBLIC_IMAGE_PROJECTS = ("ubuntu-os-cloud", "debian-cloud", "windows-cloud", "rhel-cloud",
                         "centos-cloud", "coreos-cloud", "fedora-cloud")

NIC_PROBES = ("e2-standard-4", "e2-standard-8", "n2-standard-4", "n2-standard-8",
              "n2d-standard-4", "c3-standard-4", "c3-standard-8", "c4-standard-4")


def _try(label, fn):
    try:
        return fn()
    except Exception as e:  # probe reports, never dies
        print(f"  {label}: UNAVAILABLE ({str(e)[:160]})")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", default=".gcp-sa.json")
    parser.add_argument("--region", default="us-central1")
    args = parser.parse_args()

    creds = load_credentials(args.credentials)
    project = creds["project_id"]
    print(f"project: {project}")
    print(f"service account: {creds['client_email']}")
    failures = []

    project_info = _try("project info",
                        lambda: gapi(creds, "GET", f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}"))
    if project_info:
        print(f"  name: {project_info.get('name')}  state: {project_info.get('lifecycleState')}")

    billing = _try("billing",
                   lambda: gapi(creds, "GET", f"https://cloudbilling.googleapis.com/v1/projects/{project}/billingInfo"))
    if billing is not None:
        if billing.get("billingEnabled"):
            print(f"  billing: ATTACHED ({billing.get('billingAccountName')})")
        else:
            print("  billing: NOT ENABLED — GCP deploys will fail until a billing account is linked")
            failures.append("billing missing")

    compute_state = _try("compute API",
                        lambda: gapi(creds, "GET",
                                     f"https://serviceusage.googleapis.com/v1/projects/{project}/services/compute.googleapis.com"))
    if compute_state is not None:
        state = compute_state.get("state")
        print(f"  compute.googleapis.com: {state or 'UNKNOWN'}"
              + ("" if state == "ENABLED" else " — enable before deploying"))
        if state != "ENABLED":
            failures.append("compute.googleapis.com not enabled")

    regions = _try("regions list", lambda: gapi(creds, "GET", "regions", params={"maxResults": 100}))
    if regions and regions.get("items") is not None:
        names = [r["name"] for r in regions["items"]]
        print(f"  compute readable: {len(names)} regions (IAM on the SA works)")
        us = [n for n in names if n.startswith("us-") or n.startswith("northamerica-")]
        print(f"  {len(us)} US regions: {' '.join(us[:12])}{' …' if len(us) > 12 else ''}")
    elif regions is not None:
        print("  compute regions: EMPTY/forbidden — the SA cannot read compute; grant it Compute Admin")
        failures.append("compute unreadable")

    region = _try(f"region {args.region}", lambda: gapi(creds, "GET", f"regions/{args.region}"))
    if region and region.get("quotas"):
        print(f"  {args.region} quotas:")
        for q in region["quotas"]:
            metric = q.get("metric")
            if q["usage"] > 0 or metric in ("CPUS", "DISKS_TOTAL_GB", "NETWORKS",
                                            "SUBNETWORKS_PER_NETWORK", "IN_USE_ADDRESSES"):
                print(f"    {metric:28s} {q['usage']:8.0f} / {q['limit']:.0f}")

    project_quotas = _try("global quotas", lambda: gapi(creds, "GET", ""))
    if project_quotas and project_quotas.get("quotas"):
        for q in project_quotas["quotas"]:
            if q["metric"] in ("NETWORKS", "SUBNETWORKS", "FIREWALLS", "IMAGES",
                               "SNAPSHOTS", "MACHINE_IMAGES", "INSTANCE_GROUPS"):
                print(f"    global {q['metric']:17s} {q['usage']:8.0f} / {q['limit']:.0f}")

    zone = f"{args.region}-a"
    types = _try("machine types",
                 lambda: gapi(creds, "GET", f"zones/{zone}/machineTypes", params={"maxResults": 500}))
    if types and types.get("items") is not None:
        # This project's machineTypes view does not expose `maximumNetworkInterfaces`, so the
        # documented per-family ceilings (8 on e2/n2/C-series) govern via gcp config and the
        # create call's own refusal, never a guessed readback here.
        by_name = {t["name"]: t for t in types["items"]}
        for name in NIC_PROBES:
            t = by_name.get(name)
            if t:
                print(f"    {name:14s} guestCpus={t['guestCpus']}  memMb={t['memoryMb']}  "
                      f"maxNICs={t.get('maximumNetworkInterfaces') or 'n/a'}")

    networks = _try("networks", lambda: gapi(creds, "GET", "global/networks"))
    if networks and networks.get("items") is not None:
        print(f"  existing networks: {[n['name'] for n in networks['items']]}")

    print("  stock images (newest first; family names are what gcp.image_map pins):")
    for image_project in PUBLIC_IMAGE_PROJECTS:
        catalog = _try(f"    {image_project}",
                       lambda image_project=image_project:
                       gapi(creds, "GET", f"{COMPUTE_ROOT}/projects/{image_project}/global/images",
                            params={"orderBy": "creationTimestamp desc", "maxResults": 6}))
        if catalog and catalog.get("items") is not None:
            for img in catalog["items"][:3]:
                print(f"    {image_project:16s} {img['name']}  family={img.get('family')}")

    print()
    if failures:
        print("PROBE FINDINGS (fix before a GCP deploy):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PROBE OK — target usable; pin region/zone in the estate config")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
