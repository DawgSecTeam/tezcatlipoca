"""Thin compute-API mechanics: operation waits, list-by-label, typed fetches.

Everything here takes the credentials dict from `client.load_credentials` via
`session()`, so tests can swap the whole API surface at one seam.
"""

from providers.gcp.client import gapi


def session():
    from estate import config
    from providers.gcp.client import load_credentials
    return load_credentials(config.gcp_credentials_path())


def zone():
    from estate import config
    return config.active().gcp.zone


def zone_and(instance_name):
    """`zones/<zone>/instances/<name>` for route next-hops and URLs built from names."""
    return f"zones/{zone()}/instances/{instance_name}"


def region():
    from estate import config
    return config.active().gcp.zone.rsplit("-", 1)[0]


def wait_op(creds, operation, timeout=900, poll=5.0):
    """Zone/global/regional operation poll; a failed operation raises with its error."""
    import time
    deadline = time.time() + timeout
    while True:
        if operation.get("error"):
            err = operation["error"]
            # The async failures that matter arrive as errors[] entries, and a bare
            # err.get('code') printed "None: " while the real reason sat one level down
            # (live: IP_IN_USE listed only under errors).
            details = "; ".join(f"{e.get('code')}: {e.get('message')}"
                                for e in err.get("errors", [])) \
                or str(err)[:300]
            raise RuntimeError(f"GCP operation failed: {details[:400]}")
        if operation.get("status") == "DONE":
            return operation
        if time.time() > deadline:
            raise RuntimeError(f"GCP operation {operation.get('name', '?')} did not "
                               f"finish within {timeout}s (status {operation.get('status')})")
        time.sleep(poll)
        target = operation.get("selfLink")
        if not target:
            break
        import requests
        from providers.gcp.client import access_token
        response = requests.get(target, headers={
            "Authorization": f"Bearer {access_token(creds)}"}, timeout=60)
        response.raise_for_status()
        operation = response.json()
    raise RuntimeError("GCP operation has no selfLink to poll")


def instances(creds, label_filter=None, zone_name=None):
    """List instances (this zone). label_filter: [(key, value), ...] — label-filtered."""
    params = {"maxResults": 500}
    if label_filter:
        params["filter"] = " ".join(f"labels.{k}={v}" for k, v in label_filter)
    scoped = f"zones/{zone_name or zone()}/instances"
    return gapi(creds, "GET", scoped, params=params).get("items") or []


def instance(creds, name, zone_name=None):
    try:
        return gapi(creds, "GET", f"zones/{zone_name or zone()}/instances/{name}")
    except RuntimeError as e:
        if "404" in str(e):
            return None
        raise


def images(creds, label_filter=None):
    params = {"maxResults": 500}
    if label_filter:
        params["filter"] = " ".join(f"labels.{k}={v}" for k, v in label_filter)
    return gapi(creds, "GET", "global/images", params=params).get("items") or []


def image(creds, name):
    try:
        return gapi(creds, "GET", f"global/images/{name}")
    except RuntimeError as e:
        if "404" in str(e):
            return None
        raise


def newest_family_image(creds, image_project, family):
    """The canonical by-family lookup: newest non-deprecated image of the family."""
    return gapi(creds, "GET",
                f"https://compute.googleapis.com/compute/v1/projects/{image_project}/"
                f"global/images/family/{family}")


def machine_image(creds, name):
    # Machine images are a GLOBAL resource (the only live route is
    # projects/<id>/global/machineImages; a regions/<r>/machineImages path is a
    # frontend 404 — probed live, the HTML error page, not a JSON resource error).
    try:
        return gapi(creds, "GET", f"global/machineImages/{name}")
    except RuntimeError as e:
        if "404" in str(e):
            return None
        raise


def machine_images(creds, label_filter=None):
    params = {"maxResults": 500}
    if label_filter:
        params["filter"] = " ".join(f"labels.{k}={v}" for k, v in label_filter)
    return gapi(creds, "GET", "global/machineImages", params=params).get("items") or []


def network(creds, name):
    try:
        return gapi(creds, "GET", f"global/networks/{name}")
    except RuntimeError as e:
        if "404" in str(e):
            return None
        raise


def subnetwork(creds, name):
    try:
        return gapi(creds, "GET", f"regions/{region()}/subnetworks/{name}")
    except RuntimeError as e:
        if "404" in str(e):
            return None
        raise


def firewall_rules(creds, network_link, prefix_tag):
    rules = gapi(creds, "GET", "global/firewalls", params={"maxResults": 500}).get("items") or []
    return [r for r in rules if r.get("network") == network_link
            and (r.get("labels") or {}).get("tz-range") == "1"
            and (r.get("labels") or {}).get("tz-comp") == prefix_tag]


def address(creds, name):
    try:
        return gapi(creds, "GET", f"regions/{region()}/addresses/{name}")
    except RuntimeError as e:
        if "404" in str(e):
            return None
        raise


def delete_op(creds, resource_url):
    """DELETE an absolute resource URL, wait its operation, tolerate 404."""
    try:
        operation = gapi(creds, "DELETE", resource_url)
    except RuntimeError as e:
        if "404" in str(e):
            return False
        raise
    wait_op(creds, operation)
    return True
