import os

from scrim import core


def blue_ep(args, n):
    """Per-team blue endpoint (--blue{N}-base-url/--blue{N}-model); shared endpoint fallback."""
    base = getattr(args, f"blue{n}_base_url", None)
    if base:
        return base, (getattr(args, f"blue{n}_model", None) or args.blue_model)
    return args.blue_base_url, args.blue_model


def api_key(local=False):
    for var in ("OPENROUTER_API_KEY",):
        if os.environ.get(var):
            return os.environ[var]
    env_file = core.BAD_AUTO / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("BAuto_LLM_API_KEY="):
                return line.split("=", 1)[1].strip()
    if local:
        return "local"
    raise RuntimeError("no API key: set OPENROUTER_API_KEY or BAuto_LLM_API_KEY in bad-auto/.env")


def provider_key(base_url):
    return "scrim-llm" if core.is_local_endpoint(base_url) else "openrouter"


def effort_json(effort):
    """opencode.jsonc fragment requesting a reasoning effort for the model."""
    if not effort:
        return ""
    return f',\n          "options": {{ "reasoning_effort": "{effort}" }}'
