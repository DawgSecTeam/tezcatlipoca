"""Policy: the ROE bounds as behaviour, and the candidate list as arithmetic."""

import pytest

from tests.helpers import DEFAULT_COMP, FakeSensors, make_ctx
from tlaloc.errors import PolicyError
from tlaloc.policy import MODE_RANK, Policy
from tlaloc.state.world import now_iso

FIXTURE = str(DEFAULT_COMP)


def policy_for(tmp_path, *cfgs, hours=0.0, **cfg_over):
    ctx = make_ctx(tmp_path, comp_dir=FIXTURE, **cfg_over)
    policy = Policy(ctx.cfg, ctx.world, ctx.intel, sensors=ctx.sensors, rng=ctx.rng)
    ctx.policy = policy
    # put the event clock where the test wants it
    ctx.world.ensure_event_start()
    ctx.world.meta["event_start"] -= hours * 3600
    ctx.world.save()
    return ctx, policy


def test_ladder_tiers_follow_event_progress(tmp_path):
    ctx, policy = policy_for(tmp_path)
    duration = ctx.cfg["red"]["duration_min"]
    seen = {}
    for fraction in (0.05, 0.2, 0.6, 0.75, 0.9):
        ctx.world.meta["event_start"] = policy._now() - fraction * duration * 60
        seen[fraction] = policy.ladder()["name"]
    assert seen[0.05] == "observe"
    assert seen[0.2] == "easy"
    assert seen[0.6] == "medium"
    assert seen[0.75] == "hard"
    assert seen[0.9] == "brutal"


def test_ladder_pause_after_restore_holds_the_clock(tmp_path):
    ctx, policy = policy_for(tmp_path)
    duration = ctx.cfg["red"]["duration_min"]
    ctx.world.meta["event_start"] = policy._now() - 0.25 * duration * 60  # easy tier
    assert policy.ladder()["name"] == "easy"
    ctx.world.add_blue_event("Blue1", "Web")
    tier = policy.ladder()
    assert tier["name"] == "observe" and tier["held"] is True


def test_impact_grace_and_caps(tmp_path):
    ctx, policy = policy_for(tmp_path)
    assert policy.impact_started() is False              # observe tier
    assert policy.can_impact("Blue1")[0] is False
    duration = ctx.cfg["red"]["duration_min"]
    ctx.world.meta["event_start"] = policy._now() - 0.25 * duration * 60
    assert policy.impact_started() is True
    policy.update_counts({"Blue1": (5, 1)})
    assert policy.can_impact("Blue1")[0] is True
    policy.update_counts({"Blue1": (5, 6)})              # over the cap
    assert policy.can_impact("Blue1")[0] is False
    policy.update_counts({"Blue1": (1, 0)})              # at the standing floor
    ok, why = policy.can_impact("Blue1")
    assert ok is False and "floor" in why


def test_unrecoverable_unlocks_late_or_at_the_top_tier(tmp_path):
    ctx, policy = policy_for(tmp_path)
    duration = ctx.cfg["red"]["duration_min"]
    ctx.world.meta["event_start"] = policy._now() - 0.2 * duration * 60
    assert policy.unrecoverable_unlocked() is False
    ctx.world.meta["event_start"] = policy._now() - 0.6 * duration * 60
    assert policy.unrecoverable_unlocked() is True


def test_teardown_window_suspends_the_pacing_gates(tmp_path):
    ctx, policy = policy_for(tmp_path)
    duration = ctx.cfg["red"]["duration_min"]
    window = ctx.cfg["red"]["windows"]["endgame_teardown"]
    ctx.world.meta["event_start"] = policy._now() - (duration - window + 1) * 60
    assert policy.teardown_active() is True
    assert policy.event_phase() == "endgame"
    assert policy.can_impact("Blue1") == (True, "final window — the board goes down")
    # the cap is whatever the comp/config says (0 = no cap at all)
    cap = int(ctx.cfg["red"]["caps"]["max_down"])
    assert policy.max_down_now() == cap
    assert policy.cap_label() == ("no cap" if cap == 0 else str(cap))
    assert set(policy.allowed_impact_modes()) == {"stop", "stop_mask", "stop_rename",
                                                 "stop_break_config",
                                                 "stop_rename_config"}


def test_lockout_window_only_near_the_end(tmp_path):
    ctx, policy = policy_for(tmp_path)
    assert policy.window_open("lockout") is False
    duration = ctx.cfg["red"]["duration_min"]
    ctx.world.meta["event_start"] = policy._now() - (duration - 1) * 60
    assert policy.window_open("lockout") is True


def test_recon_cutoff(tmp_path):
    ctx, policy = policy_for(tmp_path)
    assert policy.recon_allowed() is True
    duration = ctx.cfg["red"]["duration_min"]
    ctx.world.meta["event_start"] = policy._now() - 0.95 * duration * 60
    assert policy.recon_allowed() is False


def test_strongest_mode_tracks_the_tier(tmp_path):
    ctx, policy = policy_for(tmp_path)
    duration = ctx.cfg["red"]["duration_min"]
    ctx.world.meta["event_start"] = policy._now() - 0.2 * duration * 60
    assert policy.strongest_impact_mode() == "stop"
    ctx.world.meta["event_start"] = policy._now() - 0.6 * duration * 60
    assert policy.strongest_impact_mode() == "stop_mask"
    assert MODE_RANK["stop"] < MODE_RANK["stop_mask"]


def test_candidates_are_deterministic_and_bounded(tmp_path):
    ctx, policy = policy_for(tmp_path)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root",
                           escalation="sudo")
    first = policy.candidates(ctx)
    assert first, "a rooted box in a live event must produce candidates"
    ids = [c["id"] for c in first]
    assert ids == [c["id"] for c in policy.candidates(ctx)]
    assert all({"id", "effect", "params", "score", "reason"} <= set(c) for c in first)
    # obeys the ladder: no takedown mechanism above the tier
    for candidate in first:
        if candidate["effect"] == "takedown":
            assert candidate["params"]["mode"] in policy.allowed_impact_modes()


def test_candidates_prefer_evicted_boxes_and_replants(tmp_path):
    ctx, policy = policy_for(tmp_path)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root",
                           escalation="sudo")
    candidates = {c["id"]: c for c in policy.candidates(ctx)}
    assert any(c["effect"] == "reestablish" for c in candidates.values())
    # a dead artifact on a rooted box becomes a re-plant candidate
    ctx.world.add_artifact("192.168.1.10", "persist-cron", "cron (/etc/cron.d/x)",
                           check_cmd="test -f /etc/cron.d/x")
    from tlaloc.state.world import artifact_key
    ctx.world.set_dead_artifacts([artifact_key(ctx.world.artifacts()[0])])
    candidates = [c for c in policy.candidates(ctx) if c["effect"] == "persist"]
    assert candidates and "re-plant" in candidates[0]["reason"]


def test_validate_rejects_out_of_scope_and_unlocked_mechanisms(tmp_path):
    ctx, policy = policy_for(tmp_path)
    with pytest.raises(PolicyError):
        policy.validate({"effect": "recon", "params": {"ip": "10.0.0.5"}}, ctx)
    with pytest.raises(PolicyError):
        policy.validate({"effect": "takedown",
                         "params": {"ip": "192.168.1.10", "service": "nginx",
                                    "mode": "stop_rename_config"}}, ctx)
    with pytest.raises(PolicyError):
        policy.validate({"effect": "lockout", "params": {"ip": "192.168.1.10"}}, ctx)
    with pytest.raises(PolicyError):
        policy.validate({"effect": "block_port",
                         "params": {"ip": "192.168.1.10", "port": 80}}, ctx)


def test_question_bundle_is_bounded_and_typed(tmp_path):
    ctx, policy = policy_for(tmp_path)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root")
    candidates = policy.candidates(ctx)
    digest = policy.frame(ctx, candidates)
    questions, meta = policy.question_bundle(digest, candidates, ctx,
                                             include_blue_check=True)
    assert questions["next_effect"]["type"] == "choice"
    assert len(questions["next_effect"]["criteria"]) <= 9        # 8 options + hold
    assert questions["pressure_count"]["type"] == "score"
    assert questions["escalate_now"]["type"] == "noul"
    assert questions["blue_hunting"]["type"] == "noul"
    assert set(meta["options"]) >= {"hold"}
    # every criterion key is a real candidate id (nothing invented)
    for key in questions["next_effect"]["criteria"]:
        assert key == "hold" or key in meta["options"]
    # Every question at once must fit the endpoint's batch on its own: that
    # batch is 16k, so the full bundle is the normal path and the
    # decider's compaction cascade is the fallback, not the default.
    from tlaloc.decide import Decider, StubClef, tokens_needed
    assert tokens_needed(digest, questions) <= ctx.cfg["clef"]["max_request_tokens"]
    decider = Decider(ctx.cfg, ctx.world, ctx.log, clef=StubClef(), policy=policy)
    sized_digest, sized, _ = decider.compose(ctx, candidates, recent=["x"], cycle=1)
    assert "next_effect" in sized
    assert tokens_needed(sized_digest, sized) <= ctx.cfg["clef"]["max_request_tokens"]


def test_candidates_never_offer_an_effect_the_deployment_lacks(tmp_path):
    """A phased build (or a partial deployment) must not produce candidates the
    loop cannot execute: the offer list is filtered against the live registry."""
    from tlaloc import effects as effects_mod
    ctx, policy = policy_for(tmp_path)
    full = {c["effect"] for c in policy.candidates(ctx)}
    assert full, "the fixture board must offer something"
    victim = sorted(full)[0]
    saved = dict(effects_mod.REGISTRY)
    effects_mod.REGISTRY.pop(victim)
    try:
        offered = {c["effect"] for c in policy.candidates(ctx)}
    finally:
        effects_mod.REGISTRY.clear()
        effects_mod.REGISTRY.update(saved)
    assert victim not in offered
    assert offered == full - {victim}


def test_focus_team_rotates_through_the_bag(tmp_path):
    ctx, policy = policy_for(tmp_path)
    seen = []
    for _ in range(4):
        seen.append(policy.focus_team())
        ctx.world.meta["focus_since"] = 0        # force the clock to rotate
        ctx.world.save()
    assert set(seen) == {"Blue1", "Blue2"}
    assert seen[0] != seen[1], "the bag must not repeat the same team twice running"


def test_sensors_restore_detection(tmp_path):
    ctx, policy = policy_for(tmp_path)
    sensors = FakeSensors({"Blue1": {"Web": False, "SSH": True}})
    ctx.sensors = sensors
    policy.sensors = sensors
    sensors.service_map = {"Blue1": {"Web": True, "SSH": True}}
    from tlaloc.sensors import diff_restores
    assert diff_restores({"Blue1": {"Web": False, "SSH": True}},
                         {"Blue1": {"Web": True, "SSH": True}}) == \
        [{"team": "Blue1", "service": "Web"}]
    assert diff_restores({"Blue1": {"Web": False}}, {"Blue1": {}}) == []


def test_there_is_no_concurrent_down_cap(tmp_path):
    """Owner directive: red puts down as many services as it can reach."""
    ctx, policy = policy_for(tmp_path)
    ctx.cfg["red"]["caps"]["max_down"] = 0
    ctx.world.meta["event_start"] = policy._now() - 20 * 60
    policy.update_counts({"Blue1": (10, 9)})
    assert policy.max_down_now() == 0
    assert policy.can_impact("Blue1")[0] is True


def test_rotation_skips_a_service_that_is_still_down_and_verifies_a_restored_one(tmp_path):
    ctx, policy = policy_for(tmp_path)
    ip = "192.168.1.10"
    ctx.world.add_impact(ip, "nginx", "stop")
    _, why, skip = policy.pressure_rotation(ip, "nginx", True)
    assert skip is True and "already down" in why
    # blue brings the pin back (its label is the scoreboard's, not ours): re-hit it
    ctx.world.add_blue_event("Blue1", "web01-http", ip=ip)
    bonus, why, skip = policy.pressure_rotation(ip, "nginx", False)
    assert skip is False and bonus >= 30 and "blue touched this box" in why


def test_rotation_prefers_an_unhit_service_on_an_unhit_box(tmp_path):
    ctx, policy = policy_for(tmp_path)
    hot, cold = "192.168.1.10", "192.168.1.11"
    ctx.world.add_impact(hot, "nginx", "stop")      # the hot box has been hit
    b_hot, _, skip_hot = policy.pressure_rotation(hot, "apache", False)
    b_cold, why, skip_cold = policy.pressure_rotation(cold, "apache", False)
    assert not skip_hot and not skip_cold
    assert b_cold > b_hot and "not hit yet" in why


def test_a_live_callback_is_found_by_box_id_not_by_ip(tmp_path):
    """The world's c2 map is keyed <box>-<team>; asking by IP said "no channel"."""
    ctx, policy = policy_for(tmp_path)
    box = next(iter(ctx.intel.boxes()))
    box_id = f"{box['box']}-{box['identifier']}"
    ctx.world.note_callback(box_id, now_iso(), agent="poseidon")
    assert policy._channel_fresh(box["ip"]) is True
    assert policy.world.callback_age(policy._box_id_for_ip(box["ip"])) is not None
