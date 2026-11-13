

def test_the_operator_config_provider_id_matches_the_call_argv():
    # opencode resolves "<provider>/<model>" against CONFIGURED providers first;
    # a template whose provider name differs from endpoints.provider_key()
    # silently falls through to a BUILT-IN provider (wrong key, wrong auth).
    from scrim import red_c3rberus, endpoints
    base = "https://litellm.dawgsec.com/v1"
    cfg = (red_c3rberus.OPENCODE_CFG
           .replace("{PROVIDER_KEY}", endpoints.provider_key(base))
           .replace("{BASE_URL}", base)
           .replace("{API_KEY_FIELD}", "{env:TEZ_LITELLM_API_KEY}")
           .replace("{MODEL_ID}", "qwen3.8-flash-next-alt"))
    assert '"%s": {' % endpoints.provider_key(base) in cfg
    assert "tez:" not in cfg


def test_the_operator_config_renders_as_valid_json():
    # opencode's project-config loader PARSES or DROPS (silently): an unquoted
    # key — legal jsonc to a human, invalid JSON to the loader — loses the
    # whole provider block and every cycle dies 'Model unavailable'
    # (run-c1cabbd9 red lane, 5 cycles). Blue's config was quoted from the start.
    import json as _json
    from scrim import red_c3rberus
    rendered = (red_c3rberus.OPENCODE_CFG
                .replace("{PROVIDER_KEY}", "litellm")
                .replace("{BASE_URL}", "https://example.invalid/v1")
                .replace("{API_KEY_FIELD}", "{env:TEZ_LITELLM_API_KEY}")
                .replace("{MODEL_ID}", "some-model"))
    _json.loads(rendered)
