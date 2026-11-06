"""Read-only probe of a GCP deploy target: identity, billing, APIs, quotas, NIC
ceilings, stock images. Creates nothing; safe to run any time.

Usage: python3 tools/gcp-probe.py [--credentials .gcp-sa.json] [--region us-central1]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from providers.gcp.client import COMPUTE_ROOT, gapi, load_credentials  # noqa: E402

PUBLIC_IMAGE_PROJECTS = (
    ("ubuntu", "ubuntu-os-cloud", "family eq ubuntu-2404-lts-amd"),
    ("debian", "debian-cloud", "family eq debian-12"),
    ("windows", "windows-cloud", "family eq windows-2022"),
    ("rhel", "rhel-cloud", "family eq rhel-9"),
    ("centos", "centos-cloud", "family eq centos-stream-9"),
    ("fedora", "fedora-cloud", None),
    ("alpine", "alpine-cloud", None),
)

NIC_PROBES = ("e2-standard-4", "e2-standard-8", "n2-standard-4", "n2-standard-8",
              "n2d-standard-4", "c3-standard-4", "c3-standard-8", "c4-standard-4")


def _image_params(filt):
    params = {"orderBy": "creationTimestamp desc", "maxResults": 5}
    if filt:
        params["filter"] = filt
        params["maxResults"] = 3
    return params


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

    services = _try("services",
                    lambda: gapi(creds, "GET", f"https://serviceusage.googleapis.com/v1/projects/{project}/services",
                                params={"filter": "state:ENABLED", "pageSize": 200}))
    enabled = set()
    if services:
        enabled = {s["name"].removeprefix("googleapis.com/") if "/" in s["name"] else s["name"]
                   for s in services.get("items", [])}
        names = sorted(n for n in enabled if "compute" in n or "oslogin" in n or "billing" in n)
        print(f"  relevant enabled APIs: {', '.join(names) or 'none'}")
        if "compute.googleapis.com" not in {s["name"] for s in services.get("items", [])}:
            failures.append("compute.googleapis.com not enabled")
            print("  compute.googleapis.com: NOT ENABLED — enable before deploying")
    else:
        failures.append("serviceusage unreadable")

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
            if q["usage"] > 0 or q["name"] in ("CPUS", "DISKS_TOTAL_GB", "NETWORKS",
                                               "SUBNETWORKS_PER_NETWORK", "IN_USE_ADDRESSES"):
                print(f"    {q['name']:28s} {q['usage']:8.0f} / {q['limit']:.0f}")

    project_quotas = _try("global quotas", lambda: gapi(creds, "GET", ""))
    if project_quotas and project_quotas.get("quotas"):
        for q in project_quotas["quotas"]:
            if q["name"] in ("NETWORKS", "SUBNETWORKS", "FIREWALLS", "IMAGES",
                             "SNAPSHOTS", "MACHINE_IMAGES", "INSTANCE_GROUPS"):
                print(f"    global {q['name']:17s} {q['usage']:8.0f} / {q['limit']:.0f}")

    zone = f"{args.region}-a"
    types = _try("machine types",
                 lambda: gapi(creds, "GET", f"zones/{zone}/machineTypes", params={"maxResults": 500}))
    if types and types.get("items") is not None:
        by_name = {t["name"]: t for t in types["items"]}
        max_nics = max((t.get("maximumNetworkInterfaces") or 0) for t in types["items"])
        print(f"  {zone}: highest NIC count on any machine type: {max_nics}")
        for name in NIC_PROBES:
            t = by_name.get(name)
            if t:
                print(f"    {name:14s} NICs={t.get('maximumNetworkInterfaces')}  "
                      f"guestCpus={t['guestCpus']}  memMb={t['memoryMb']}")
        if max_nics < 8:
            failures.append(f"NIC ceiling {max_nics} in {zone} caps teams per range")

    networks = _try("networks", lambda: gapi(creds, "GET", "global/networks"))
    if networks and networks.get("items") is not None:
        print(f"  existing networks: {[n['name'] for n in networks['items']]}")

    print("  stock images (the base-image map's source pool):")
    for label, image_project, filt in PUBLIC_IMAGE_PROJECTS:
        catalog = _try(f"    {label}",
                       lambda image_project=image_project, filt=filt:
                       gapi(creds, "GET", f"{COMPUTE_ROOT}/projects/{image_project}/global/images",
                            params=_image_params(filt)))
        if catalog and catalog.get("items") is not None:
            for img in catalog["items"][:3]:
                print(f"    {label:8s} {img['name']}  status={img.get('status')}  family={img.get('family')}")

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
