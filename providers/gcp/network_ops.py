"""The range's network: one VPC per run — isolation by unrouted VPC, ownership by name.

GCP's implied rules make this simpler than the PVE bridge work: cross-subnet ingress
is denied by default between the team subnets, the red subnet is unreachable from the
teams (and vice versa), and every box's default gateway is its subnet's .1 — which is
the ENGINE's interface. The engine's iptables isolation/NAT code runs unchanged as
the only inter-subnet router. Egress rides the Cloud NAT configured here.

Firewalls do not carry labels on this API, so network objects carry their ownership
in the name (the tz-<comp>-<run8> prefix) — the same scheme the recorded inventory
uses; deletion goes through tier order so instances are gone before their subnets.
"""

from providers.gcp import compute
from providers.gcp.client import gapi
from providers.gcp import naming

# The engine's NIC0 lives here; its RESERVED external IP is the run's mgmt address.
MGMT_CIDR = "10.224.0.0/24"
MGMT_ENGINE_IP = "10.224.0.10"

ENGINE_TAG = "tz-engine"

MGMT_PORTS = ("22", "80")  # driver SSH + the scoreboard's listener


def ensure_network(document, *, comp_name, run_id, team_identifiers,
                   in_path=False, red=False, record=print):
    """Idempotent create of the whole stack; records each piece in the document."""
    creds = compute.session()
    prefix = naming.prefix(comp_name, run_id)
    labels = naming.labels(comp_name, run_id)
    vpc = naming.vpc_name(prefix)
    net = compute.network(creds, vpc)
    if net is None:
        record(f"  network: creating VPC {vpc}")
        wait = compute.wait_op(creds, gapi(creds, "POST", "global/networks", data={
            "name": vpc, "autoCreateSubnetworks": False,
            "routingConfig": {"routingMode": "REGIONAL"}}))
    document.record("network", "", vpc)

    def subnet(name, cidr, role, identifier=""):
        if compute.subnetwork(creds, name) is None:
            record(f"  network: creating subnet {name} ({cidr})")
            compute.wait_op(creds, gapi(creds, "POST", f"regions/{compute.region()}/subnetworks", data={
                "name": name, "network": f"projects/{creds['project_id']}/global/networks/{vpc}",
                "ipCidrRange": cidr, "region": compute.region(),
                "description": f"tezcatlipoca {run_id} {role}"}))
        document.record("subnet", "", name)

    mgmt = naming.subnet_name(prefix, "mgmt")
    subnet(mgmt, MGMT_CIDR, "engine mgmt (ops ingress via the reserved external IP)")

    for identifier in team_identifiers:
        team = naming.subnet_name(prefix, "t", identifier)
        subnet(team, f"192.168.{identifier}.0/24", f"team {identifier}")
        if in_path:
            transit = naming.subnet_name(prefix, "w", identifier)
            subnet(transit, f"172.31.{identifier}.0/30", f"team {identifier} transit")
    if red:
        subnet(naming.subnet_name(prefix, "red"), "10.200.0.0/24", "red segment")

    _ensure_router_nat(creds, prefix, mgmt, record)

    net_link = f"projects/{creds['project_id']}/global/networks/{vpc}"
    address = _ensure_engine_address(creds, prefix, record=record)
    _ensure_mgmt_rule(creds, prefix, net_link, labels, record)

    document.save()
    return {"vpc": vpc, "mgmt_subnet": mgmt,
            "engine_public_ip": (address or {}).get("address") or ""}


def _ensure_router_nat(creds, prefix, mgmt_subnet, record):
    router = naming.router_name(prefix)
    try:
        compute.wait_op(creds, gapi(creds, "POST", f"regions/{compute.region()}/routers", data={
            "name": router,
            "network": f"projects/{creds['project_id']}/global/networks/{naming.vpc_name(prefix)}",
            "region": compute.region()}))
    except RuntimeError as e:
        if "alreadyExists" not in str(e):
            raise
    # Cloud NAT has no idempotent PUT here: a NAT config that already exists fails the
    # PATCH with 'already exists' naming the range, which is the adopt path.
    try:
        compute.wait_op(creds, gapi(creds, "PATCH", f"regions/{compute.region()}/routers/{router}", data={
            "name": router, "region": compute.region(),
            "nats": [{"name": naming.nat_name(prefix),
                      "natIpAllocateOption": "AUTO_ONLY",
                      "sourceSubnetworkIpRangesToNat": "ALL_SUBNETWORKS_ALL_IP_RANGES"}]}))
    except RuntimeError as e:
        if "already exists" not in str(e).lower():
            raise
    record(f"  network: Cloud NAT ready on {router}")
    return router


def _ensure_engine_address(creds, prefix, record=print):
    name = naming.address_name(prefix, "engine")
    found = compute.address(creds, name)
    if found is None:
        record(f"  network: reserving the engine's public address {name}")
        compute.wait_op(creds, gapi(creds, "POST", f"regions/{compute.region()}/addresses", data={
            "name": name, "addressType": "EXTERNAL", "region": compute.region()}))
        found = compute.address(creds, name)
    return found


def _ensure_mgmt_rule(creds, prefix, net_link, labels, record):
    from estate import config
    sources = [s.strip() for s in (config.active().get("gcp.ingress_sources") or "").split(",")
               if s.strip()]
    rule = naming.firewall_name(prefix, "mgmt")
    if not sources:
        raise SystemExit(
            "  ERROR: gcp.ingress_sources is not set — the driver could not reach the range. "
            "It is a comma list of the OPERATOR egress addresses allowed onto the engine's "
            "SSH and scoreboard (set TEZ_GCP_INGRESS_SOURCES or the profile's [gcp]).")
    listed = gapi(creds, "GET", "global/firewalls", params={"maxResults": 500}).get("items") or []
    if any(r["name"] == rule for r in listed):
        return rule
    record(f"  network: mgmt firewall {rule} (sources {', '.join(sources)})")
    compute.wait_op(creds, gapi(creds, "POST", "global/firewalls", data={
        "name": rule, "network": net_link,
        "direction": "INGRESS", "allowed": [{"IPProtocol": "tcp", "ports": list(MGMT_PORTS)}],
        "sourceRanges": sources, "targetTags": [ENGINE_TAG],
        "description": f"tezcatlipoca mgmt — {rule}"}))
    return rule


def delete_network(creds, vpc, log=print):
    """Subnets (firewalls die with the network) out first — GCP refuses an in-use subnet."""
    net = compute.network(creds, vpc)
    if net is None:
        return False
    prefixes = f"projects/{creds['project_id']}/global/networks/{vpc}"
    listed = gapi(creds, "GET", f"regions/{compute.region()}/subnetworks",
                  params={"maxResults": 500}).get("items") or []
    for sub in [s for s in listed if s["network"] == prefixes]:
        compute.delete_op(creds, sub["selfLink"])
        log(f"  destroyed subnet {sub['name']}")
    routers = gapi(creds, "GET", f"regions/{compute.region()}/routers",
                   params={"maxResults": 500}).get("items") or []
    for router in [r for r in routers if r["network"] == prefixes]:
        compute.delete_op(creds, router["selfLink"])
        log(f"  destroyed router {router['name']}")
    addresses = gapi(creds, "GET", f"regions/{compute.region()}/addresses",
                     params={"maxResults": 500}).get("items") or []
    for addr in [a for a in addresses if a["name"].startswith(vpc)]:
        compute.delete_op(creds, addr["selfLink"])
        log(f"  destroyed address {addr['name']}")
    compute.delete_op(creds, net["selfLink"])
    log(f"  destroyed network {vpc}")
    return True
