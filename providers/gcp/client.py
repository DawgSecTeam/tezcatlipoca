"""GCP REST plumbing: service-account JWT auth and raw API calls.

No google-api-python-client by design: the deploy host ships no gcloud and no
Google SDK, and the compute surface this backend uses is small and stable.
"""

import base64
import json
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
COMPUTE_ROOT = "https://compute.googleapis.com/compute/v1"

# token -> (access_token, expires_at); one SA per process is the only case there is
_TOKENS = {}


def load_credentials(path):
    with open(path) as fh:
        return json.load(fh)


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _service_jwt(creds):
    key = serialization.load_pem_private_key(creds["private_key"].encode(), password=None)
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    now = int(time.time())
    body = _b64url(json.dumps({
        "iss": creds["client_email"], "scope": SCOPE, "aud": TOKEN_URL,
        "iat": now, "exp": now + 3600,
    }).encode())
    signing_input = header + b"." + body
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input + b"." + _b64url(signature)


def access_token(creds, min_ttl=120):
    cached = _TOKENS.get(creds["client_email"])
    if cached and cached[1] - time.time() > min_ttl:
        return cached[0]
    response = requests.post(TOKEN_URL, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": _service_jwt(creds).decode(),
    }, timeout=30)
    response.raise_for_status()
    payload = response.json()
    _TOKENS[creds["client_email"]] = (payload["access_token"],
                                      time.time() + payload["expires_in"])
    return payload["access_token"]


def gapi(creds, method, path, *, params=None, data=None, timeout=120):
    """A compute path like `regions/us-central1`, ``, or a bare absolute URL."""
    if path.startswith("http"):
        url = path
    else:
        url = f"{COMPUTE_ROOT}/projects/{creds['project_id']}" + (f"/{path}" if path else "")
    response = requests.request(method, url, params=params, json=data,
                                headers={"Authorization": f"Bearer {access_token(creds)}"},
                                timeout=timeout)
    if response.status_code >= 300:
        raise RuntimeError(f"GCP {method} {url} -> {response.status_code}: {response.text[:400]}")
    return response.json() if response.content else {}


def gapi_all(creds, path, *, params=None):
    """Every page of a list endpoint: a shared project's collections run past one
    maxResults=500 page, and a silently truncated list reads as "absent"."""
    items, params = [], dict(params or {})
    params["maxResults"] = 500
    while True:
        page = gapi(creds, "GET", path, params=params)
        items.extend(page.get("items") or [])
        if not page.get("nextPageToken"):
            return items
        params["pageToken"] = page["nextPageToken"]
