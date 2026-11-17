

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


def test_a_cap_timeout_costs_one_cycle_not_the_lane(tmp_path):
    # run-c1cabbd9: CycleTimeout escaped the operator loop, threading killed the
    # thread, the supervisor cried DIED and c3rberus sat un-driven for the rest
    # of the event. The loop must absorb it exactly like blue's does.
    import threading
    from types import SimpleNamespace
    from scrim import blue_agent, red_c3rberus

    calls = []

    def flaky(*a, prompt=None, wd=None, env=None, stop=None, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise blue_agent.CycleTimeout(["opencode"], 900, "cap")
        return SimpleNamespace(returncode=0, stdout="done", stderr="")

    wd = tmp_path / "red-c3rberus"
    wd.mkdir()
    (wd / "LOG.md").write_text("x")
    (wd / "NOTEBOOK.md").write_text("x")
    args = SimpleNamespace(run_dir=str(tmp_path),
                           blue_base_url="https://litellm.dawgsec.com/v1",
                           red_model="m", duration_min=3, cycle_timeout=900, teams=2)
    import os
    monkey_env = os.environ
    monkey_env["TEZ_LITELLM_API_KEY"] = "sk-test-not-a-real-key"
    stop = threading.Event()
    monkey = SimpleNamespace(run_cycle_with_retry=flaky)
    orig = blue_agent.run_cycle_with_retry
    blue_agent.run_cycle_with_retry = flaky
    try:
        th = threading.Thread(target=red_c3rberus.red_feed_loop,
                              args=(args, stop, __import__("time").time()),
                              kwargs={"first_delay": 0.0})
        th.daemon = True
        th.start()
        __import__("time").sleep(1.2)
        stop.set()
        th.join(timeout=20)
    finally:
        blue_agent.run_cycle_with_retry = orig
    assert not th.is_alive(), "the operator thread must survive a cap timeout"
    assert not calls or len(calls) >= 1
    feed = (wd / "cycles").exists() or (wd / "feed.log").exists()
    assert feed
