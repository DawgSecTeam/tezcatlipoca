import os

from scrim import core
from scrim import llm_endpoints


# per-team endpoint attrs come from --blue{N}-base-url/--blue{N}-model; shared fallback
def blue_ep(args, n):
    base = getattr(args, f"blue{n}_base_url", None)
    if base:
        return base, (getattr(args, f"blue{n}_model", None) or args.blue_model)
    return args.blue_base_url, args.blue_model


def api_key(base_url=None, local=None):
    """The API key for an endpoint — resolved from the table, never invented.

    A base_url naming a PUBLIC table entry MUST have its key in the environment: a missing
    `DEEPSEEK_API_KEY` raises here instead of degrading to the literal "local" (which reached
    blue as a 401 on every cycle, and red as a bogus bearer token — an "empty room" that looked
    like a working defense).

    No base_url, or a host that is not in the table (operator-side llama.cpp / a tailnet LLM
    host), keeps the
    legacy order: `OPENROUTER_API_KEY`, then bad-auto/.env's `BAuto_LLM_API_KEY`, then the
    literal "local" when `local=True` (a local llama.cpp ignores whatever it is sent).
    """
    ep = llm_endpoints.lookup(base_url) if base_url else None
    if ep and ep.kind == llm_endpoints.PUBLIC:
        key = (os.environ.get(ep.key_env) or "").strip()
        if key:
            return key
        raise RuntimeError(
            f"{ep.name}: {ep.key_env} is not set — refusing to drive {ep.base_url} without its "
            "key (set it where the driver reads env, e.g. ~/tez-engine.env on the run host)")
    for var in ("OPENROUTER_API_KEY",):
        if os.environ.get(var):
            return os.environ[var]
    env_file = core.BAD_AUTO / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("BAuto_LLM_API_KEY="):
                return line.split("=", 1)[1].strip()
    if local or (base_url is not None and ep is None):
        return "local"
    raise RuntimeError("no API key: set OPENROUTER_API_KEY or BAuto_LLM_API_KEY in bad-auto/.env")


def provider_key(base_url):
    """The provider name blue's opencode.jsonc declares: the table's, else the local one."""
    return llm_endpoints.provider_name(base_url)


def key_env_name(base_url):
    """Env var blue must export for this endpoint; "" means operator-side (it has no key)."""
    return llm_endpoints.key_env(base_url)


def effort_json(effort):
    if not effort:
        return ""
    return f',\n          "options": {{ "reasoning_effort": "{effort}" }}'
