"""The LLM endpoint table: what kind of endpoint a base_url names, and where its key lives."""

import os
from dataclasses import dataclass
from urllib.parse import urlparse

PUBLIC = "public"
OPERATOR = "operator"
# An operator-side gateway (LiteLLM, a relay in front of many models) can require a key too.
OPERATOR_KEY_ENV = "TEZ_SCRIM_API_KEY"


@dataclass(frozen=True)
class Endpoint:
    name: str            # provider name blue's opencode.jsonc declares
    base_url: str
    kind: str = PUBLIC
    key_env: str = ""    # env var carrying the key; never a literal fallback
    models: tuple = ()   # ids the endpoint advertises (documentation + preflight)
    note: str = ""


# Model ids are the endpoint's own (GET /v1/models); a gateway re-ids them and needs its own entry.
ENDPOINTS = (
    Endpoint(name="deepseek", base_url="https://api.deepseek.com/v1", kind=PUBLIC,
             key_env="DEEPSEEK_API_KEY", models=("deepseek-flash", "deepseek-v4-pro"),
             note="official DeepSeek API (OpenAI-compatible)"),
    Endpoint(name="openrouter", base_url="https://openrouter.ai/api/v1", kind=PUBLIC,
             key_env="OPENROUTER_API_KEY",
             note="cloud aggregator"),
)


def _host(base_url):
    raw = (base_url or "").strip()
    if not raw:
        return ""
    if "//" not in raw:
        raw = "http://" + raw
    try:
        return (urlparse(raw).hostname or "").lower()
    except ValueError:
        return ""


def _norm(base_url):
    return (base_url or "").strip().rstrip("/").lower()


def lookup(base_url):
    host = _host(base_url)
    if not host:
        return None
    for ep in ENDPOINTS:
        if host == _host(ep.base_url):
            return ep
    norm = _norm(base_url)
    for ep in ENDPOINTS:
        if norm and norm.startswith(_norm(ep.base_url)):
            return ep
    return None


def is_public(base_url):
    ep = lookup(base_url)
    return bool(ep and ep.kind == PUBLIC)


def is_operator_side(base_url):
    return not is_public(base_url)


def provider_name(base_url):
    ep = lookup(base_url)
    return ep.name if ep else "scrim-llm"


def key_env(base_url):
    ep = lookup(base_url)
    if ep:
        return ep.key_env if ep.kind == PUBLIC else ""
    return OPERATOR_KEY_ENV if os.environ.get(OPERATOR_KEY_ENV) else ""


def describe(base_url):
    ep = lookup(base_url)
    if not ep:
        return f"{base_url} (operator-side: no key, may be tunnelled)"
    return (f"{ep.name} ({ep.kind}: {ep.base_url}"
            + (f", key from {ep.key_env}" if ep.key_env else ", no key") + ")")


def from_env(base_url):
    import os
    var = key_env(base_url)
    if not var:
        return None
    return (os.environ.get(var) or "").strip() or None
