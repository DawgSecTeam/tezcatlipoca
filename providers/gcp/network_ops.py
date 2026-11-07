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
RED01_TAG = "tz-red01"

MGMT_PORTS = ("22", "80")  # driver SSH + the scoreboard's listener


def ensure_network(document, *, comp_name, run_id, team_identifiers,
                   in_path=False, red=False, record=print):
    """Idempotent create of the whole stack; records each piece in the document.

    The engine's mgmt address is an EPHEMERAL external IP taken at instance-create:
    this project refuses instances.insert's STATIC regional-address attach with "not
    found in region" for addresses its own list confirms exist (every reference shape
    refused, live 2026-11-06) — the deploy records the realized address in the state
    document, same contract the proxmox flow has with its mgmt IP.
    """
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
    _ensure_mgmt_rule(creds, prefix, net_link, labels, record)
    # GCE DEFAULT-DENIES all ingress, and custom VPCs carry no default-allow-internal:
    # without these, every intra-range flow dies at the target (engine->box SSH,
    # box->apt-proxy replies) while cross-subnet packets would route STRAIGHT through
    # the fabric, around the engine's isolation DROP. The per-team source rules keep
    # team-subnet flows inside their tag, so a cross-team packet has no allow on any path.
    for identifier in team_identifiers:
        _ensure_rule(creds, prefix, f"t{identifier}", net_link,
                     source=f"192.168.{identifier}.0/24",
                     target=naming.team_tag(identifier),
                     record=record)
        _ensure_rule(creds, prefix, f"t{identifier}-to-engine", net_link,
                     source=f"192.168.{identifier}.0/24",
                     target=ENGINE_TAG,
                     record=record)
    if red:
        # red's attacks keep their source end-to-end: this admits 10.200.0.10-sized
        # SOURCES onto every team's boxes (and the engine); nothing admits a team
        # subnet toward red — same asymmetry as the engine's iptables trio.
        for identifier in team_identifiers:
            _ensure_rule(creds, prefix, f"red-to-t{identifier}", net_link,
                         source="10.200.0.0/24",
                         target=naming.team_tag(identifier),
                         record=record)
        _ensure_rule(creds, prefix, "red-to-engine", net_link,
                     source="10.200.0.0/24", target=ENGINE_TAG,
                     record=record)
        # tlaloc is staged over red01's EPHEMERAL public IP (resize's in-guest grow,
        # sensor/director pushes): the same operator sources as the engine's mgmt
        # rule, SSH only.
        _ensure_ops_rule(creds, prefix, "red-mgmt", net_link, _ops_sources(),
                         ("22",), RED01_TAG, record)

    document.save()
    return {"vpc": vpc, "mgmt_subnet": mgmt, "engine_public_ip": ""}


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


def _ensure_rule(creds, prefix, role, net_link, *, source, target, record=print):
    # No port lists: intra-range flows are port-agnostic by design (apt-proxy 3142,
    # nakon'd service ports, red's DNAT targets); the isolation is the src/tgt scope,
    # the same way a PVE team bridge has no port filters either.
    name = naming.firewall_name(prefix, role)
    listed = gapi(creds, "GET", "global/firewalls", params={"maxResults": 500}).get("items") or []
    if any(r["name"] == name for r in listed):
        return name
    record(f"  firewall  : {name} (from {source} -> {target})")
    compute.wait_op(creds, gapi(creds, "POST", "global/firewalls", data={
        "name": name, "network": net_link, "direction": "INGRESS",
        "allowed": [{"IPProtocol": proto} for proto in ("tcp", "udp", "icmp")],
        "sourceRanges": [source], "targetTags": [target],
        "description": f"tezcatlipoca intra-range — {role}"}))
    return name


def _ops_sources():
    from estate import config
    sources = [s.strip() for s in (config.active().get("gcp.ingress_sources") or "").split(",")
               if s.strip()]
    if not sources:
        raise SystemExit(
            "  ERROR: gcp.ingress_sources is not set — the driver could not reach the range. "
            "It is a comma list of the OPERATOR egress addresses allowed onto the engine's "
            "SSH and scoreboard (set TEZ_GCP_INGRESS_SOURCES or the profile's [gcp]).")
    return sources


def _ensure_ops_rule(creds, prefix, role, net_link, sources, ports, target, record):
    rule = naming.firewall_name(prefix, role)
    listed = gapi(creds, "GET", "global/firewalls", params={"maxResults": 500}).get("items") or []
    if any(r["name"] == rule for r in listed):
        return rule
    record(f"  network: ops firewall {rule} ({', '.join(ports)} from {', '.join(sources)} -> {target})")
    compute.wait_op(creds, gapi(creds, "POST", "global/firewalls", data={
        "name": rule, "network": net_link,
        "direction": "INGRESS", "allowed": [{"IPProtocol": "tcp", "ports": list(ports)}],
        "sourceRanges": sources, "targetTags": [target],
        "description": f"tezcatlipoca ops — {rule}"}))
    return rule


def _ensure_mgmt_rule(creds, prefix, net_link, labels, record):
    return _ensure_ops_rule(creds, prefix, "mgmt", net_link, _ops_sources(),
                            MGMT_PORTS, ENGINE_TAG, record)


def ensure_engine_route(creds, prefix, engine_name, team_identifiers=(), record=print):
    """Two routes make the engine the range's router.

    The 0/0-by-engine route (priority 900) is TAG-SCOPED to the team boxes: they have
    no external IPs and this is their only path out. red01 is an ops box with its own
    1:1 NAT — scoped in, its replies left through the engine, whose MASQUERADE covers
    team subnets only, so every driver dial to red01's public IP timed out while the
    firewall was provably correct (live, gcp-acc17). Scoped out, red01's egress rides
    the VPC's implied default-internet route like any GCE VM with an external IP.

    The ENGINE'S OWN traffic is NOT exempt from next-hop routes (live proof: its
    public-SSH dialed "filtered" while the route stood, replies looping into their
    own next hop), so a higher-priority TAGGED route (800, network tag tz-engine)
    sends the engine's own egress to the default internet gateway, where Cloud NAT
    picks it up.
    """
    wan = naming.route_wan_name(prefix)
    via = naming.route_name(prefix)
    # Name-match over the plain list: a filtered network= query returns nothing for
    # these routes (live 409 against a route it should have seen), so don't trust it.
    listed = gapi(creds, "GET", "global/routes", params={"maxResults": 500}).get("items") or []
    have = {r["name"] for r in listed}
    network = f"projects/{creds['project_id']}/global/networks/{naming.vpc_name(prefix)}"
    if wan not in have:
        record(f"  network: engine WAN route ({wan}, priority 800, tag {ENGINE_TAG})")
        compute.wait_op(creds, gapi(creds, "POST", "global/routes", data={
            "name": wan, "network": network,
            "destRange": "0.0.0.0/0", "priority": 800,
            "nextHopGateway": (f"https://www.googleapis.com/compute/v1/projects/"
                               f"{creds['project_id']}/global/gateways/"
                               f"default-internet-gateway"),
            "tags": [ENGINE_TAG],
            "description": "tezcatlipoca — the engine's own egress (its next-hop-route "
                           "exemption is a myth; this route keeps its replies alive)"}))
    # Routes are immutable in place: a changed team set (tags) means replace the route.
    wanted = sorted(naming.team_tag(i) for i in team_identifiers)
    existing = next((r for r in listed if r["name"] == via), None)
    if existing and sorted(existing.get("tags") or []) != wanted:
        record(f"  network: re-scoping the via-engine route to this team set")
        compute.delete_op(creds, existing["selfLink"])
        have.discard(via)
    if via not in have:
        record(f"  network: team boxes' default via the engine ({via}, priority 900, "
               f"tags {', '.join(wanted) or 'none'})")
        compute.wait_op(creds, gapi(creds, "POST", "global/routes", data={
            "name": via, "network": network,
            "destRange": "0.0.0.0/0", "priority": 900,
            "nextHopInstance": (f"https://compute.googleapis.com/compute/v1/projects/"
                                f"{creds['project_id']}/{compute.zone_and(engine_name)}"),
            "tags": wanted,
            "description": f"tezcatlipoca — scored boxes default through the engine ({prefix})"}))
    return via


def delete_network(creds, vpc, log=print):
    """Rules and routes out first, then subnets/routers — GCP refuses to delete a
    network a firewall rule still references (the "rules die with the network"
    belief was console behavior, not the API's)."""
    net = compute.network(creds, vpc)
    if net is None:
        return False
    # Subnet/router objects carry the network as a FULL URL; the short project path
    # compared here never matched and whole tiers survived the sweep (live, gcp-acc).
    def _ours(ref):
        return str(ref or "").endswith(f"/global/networks/{vpc}")

    # Routes block a network delete; sweep this range's own before the network goes.
    for route in [r for r in (gapi(creds, "GET", "global/routes",
                                   params={"maxResults": 500}).get("items") or [])
                  if (r["name"] or "").startswith(vpc)]:
        compute.delete_op(creds, route["selfLink"])
        log(f"  destroyed route {route['name']}")
    # Firewall rules reference the network and BLOCK its delete; this range's rules
    # all carry the run prefix (the VPC name), so sweep by name.
    for fw in [f for f in (gapi(creds, "GET", "global/firewalls",
                                params={"maxResults": 500}).get("items") or [])
               if (f["name"] or "").startswith(vpc)]:
        compute.delete_op(creds, fw["selfLink"])
        log(f"  destroyed firewall {fw['name']}")
    # The router first: its Cloud NAT references the subnets, and the API refuses a
    # subnet delete (and the network's) while the referencing config stands.
    routers = gapi(creds, "GET", f"regions/{compute.region()}/routers",
                   params={"maxResults": 500}).get("items") or []
    for router in [r for r in routers if _ours(r.get("network"))]:
        compute.delete_op(creds, router["selfLink"])
        log(f"  destroyed router {router['name']}")
    listed = gapi(creds, "GET", f"regions/{compute.region()}/subnetworks",
                  params={"maxResults": 500}).get("items") or []
    for sub in [s for s in listed if _ours(s.get("network"))]:
        compute.delete_op(creds, sub["selfLink"])
        log(f"  destroyed subnet {sub['name']}")
    addresses = gapi(creds, "GET", f"regions/{compute.region()}/addresses",
                     params={"maxResults": 500}).get("items") or []
    for addr in [a for a in addresses if a["name"].startswith(vpc)]:
        compute.delete_op(creds, addr["selfLink"])
        log(f"  destroyed address {addr['name']}")
    compute.delete_op(creds, net["selfLink"])
    log(f"  destroyed network {vpc}")
    return True
