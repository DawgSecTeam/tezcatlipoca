"""The LLM endpoint table: what kind of endpoint a base_url names, and where its key lives.

Why a table instead of a string test: classification used to be
`"openrouter" not in base_url` (`scrim/core.py:is_local_endpoint`), so EVERY public API that was
not openrouter.com — the official DeepSeek API included — was driven as if it were an
operator-side llama.cpp. Driving a public TLS API down that path silently produced, in a single
setup:

- blue's `opencode.jsonc` carrying the literal string ``local`` as its apiKey: a 401 on every
  cycle, which makes a broken defense indistinguishable from an empty room;
- red taking the slow-local profile (max_tokens 6144 / timeout 300 / json_retries 0);
- `red_tunnel` opening a reverse SSH forward at the endpoint's TLS port and rewriting red's
  base_url to `http://localhost:8180/v1` — plain HTTP onto a TLS port, i.e. red LLM-dead;
- a missing key degrading to the literal ``local`` instead of raising.

An entry is matched by HOST, so `https://api.deepseek.com` and `https://api.deepseek.com/v1` name
the same endpoint. Anything NOT in the table is OPERATOR-SIDE by default: that is the behaviour
llama.cpp / GX10 URLs rely on (no key, slower, reached by red01 over a reverse tunnel), so
single-host rehearsals are unchanged.

Model ids are the ENDPOINT's own. The DeepSeek entry was read from `GET /v1/models` on
2026-10-14 (`deepseek-flash`, `deepseek-v4-pro`); a gateway that fronts the same models under a
different id is a different endpoint and needs its own entry.
"""

from dataclasses import dataclass
from urllib.parse import urlparse

PUBLIC = "public"
OPERATOR = "operator"


@dataclass(frozen=True)
class Endpoint:
    name: str            # provider name blue's opencode.jsonc declares
    base_url: str
    kind: str = PUBLIC
    key_env: str = ""    # env var carrying the key; never a literal fallback
    models: tuple = ()   # ids the endpoint advertises (documentation + preflight)
    note: str = ""


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
    """The table entry a base_url names, or None (None == operator-side)."""
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
    """True for a cloud API the harness may dial directly and must send a real key to."""
    ep = lookup(base_url)
    return bool(ep and ep.kind == PUBLIC)


def is_operator_side(base_url):
    return not is_public(base_url)


def provider_name(base_url):
    """The provider name blue's opencode.jsonc declares for this endpoint."""
    ep = lookup(base_url)
    return ep.name if ep else "scrim-llm"


def key_env(base_url):
    """The env var that carries this endpoint's key ("" for operator-side: it has none)."""
    ep = lookup(base_url)
    if not ep or ep.kind != PUBLIC:
        return ""
    return ep.key_env


def describe(base_url):
    """One line for the run log: which endpoint, which kind, which key env."""
    ep = lookup(base_url)
    if not ep:
        return f"{base_url} (operator-side: no key, may be tunnelled)"
    return (f"{ep.name} ({ep.kind}: {ep.base_url}"
            + (f", key from {ep.key_env}" if ep.key_env else ", no key") + ")")


def from_env(base_url):
    """The configured key for a PUBLIC endpoint, or None (never a literal)."""
    import os
    var = key_env(base_url)
    if not var:
        return None
    return (os.environ.get(var) or "").strip() or None
