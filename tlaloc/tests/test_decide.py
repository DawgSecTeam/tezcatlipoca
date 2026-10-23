"""The decision layer: thresholds, fallbacks, the arbiter, and offline replay."""

import json
from pathlib import Path

import pytest

from tests.helpers import base_cfg, make_ctx
from tlaloc import decide as decide_mod
from tlaloc.decide import Decider, StubClef, keyword_intent, top_probability
from tlaloc.errors import ClefError
from tlaloc.policy import Policy, make_candidate

RECORDED = Path(__file__).parent / "fixtures" / "decisions-recorded.jsonl"


def records():
    return [json.loads(line) for line in RECORDED.read_text().splitlines() if line.strip()]


def candidates_from(record):
    return [make_candidate(c["effect"], c["params"], c["score"], c["reason"],
                           cost=c.get("cost", "low"), tag=c.get("tag"),
                           high_stakes=c.get("high_stakes"))
            for c in record["candidates"]]


class StubLLM:
    """A fixed reply, a chooser callable, or a failure."""

    def __init__(self, reply=None, chooser=None, boom=False):
        self.enabled = True
        self.reply = reply
        self.chooser = chooser
        self.boom = boom
        self.calls = []

    def choose(self, options, context=None, **kwargs):
        self.calls.append({"options": [o["id"] for o in options], "context": context})
        if self.boom:
            raise decide_mod.LLMError("llm down")
        if self.chooser is not None:
            return self.chooser(options)
        return self.reply


def decider_for(tmp_path, clef, llm=None, cfg_over=None):
    # a bare `clef=` kwarg would collide with the positional clef client
    ctx = make_ctx(tmp_path, cfg=base_cfg(**(cfg_over or {})))
    policy = Policy(ctx.cfg, ctx.world, ctx.intel, sensors=ctx.sensors, rng=ctx.rng)
    ctx.policy = policy
    decider = Decider(ctx.cfg, ctx.world, ctx.log, clef=clef, llm=llm, policy=policy)
    return ctx, policy, decider


def stub_for(record):
    probability = dict(record["probabilities"])
    answer = {"type": "choice", "choice": record.get("answer"),
              "probabilities": probability}
    return StubClef(answers={"next_effect": answer, "pressure_count":
                             {"type": "score", "score": 0.0,
                              "probabilities": {"0": 0.8, "1": 0.15, "2": 0.05},
                              "legend": {"0": "zero", "1": "one", "2": "two"}},
                             "escalate_now": {"type": "noul", "noul": 0.1}})


# --- the chain over recorded bundles -----------------------------------------

@pytest.mark.parametrize("record", records(), ids=lambda r: f"cycle{r['cycle']}")
def test_replay_of_recorded_bundles(tmp_path, record):
    # Replay reproduces the ENVIRONMENT that produced the record: only a chain that recorded
    # an arbiter decision had an arbiter available.
    arbiter = (StubLLM(chooser=lambda options: {"id": options[0]["id"],
                                                "why": "replay: first bounded option"})
               if record["source"] == "llm_arbiter" else None)
    ctx, policy, decider = decider_for(tmp_path, stub_for(record), llm=arbiter)
    candidates = candidates_from(record)
    decision = decider.next_effect(record["cycle"], record["state"], candidates)

    threshold = record["theta"]
    top = max(record["probabilities"].values())
    if top >= threshold:
        assert decision.source == "clef"
        assert decision.chosen is not None
        assert decision.chosen["id"] == record["chosen"]
    elif record["source"] == "llm_arbiter":
        # the arbiter is bounded to the candidate list
        assert decision.source == "llm_arbiter"
        assert decision.chosen["id"] in [c["id"] for c in candidates]
    else:
        assert decision.source == "deterministic"
        assert decision.chosen is not None
        assert decision.chosen["id"] == candidates[0]["id"]
    # the decision is logged with its whole chain
    logged = [e for e in ctx.log.read() if e["kind"] == "decision"]
    assert logged and logged[-1]["theta"] == threshold
    assert logged[-1]["source"] == decision.source


# --- the gate -----------------------------------------------------------------

def test_high_confidence_acts(tmp_path):
    ctx, policy, decider = decider_for(tmp_path, StubClef(answers={
        "next_effect": {"type": "choice", "choice": "recon:192.168.1.10",
                        "probabilities": {"recon:192.168.1.10": 0.91, "hold": 0.09}}}))
    candidates = [make_candidate("recon", {"ip": "192.168.1.10"}, 15, "scan it")]
    decision = decider.next_effect(1, {}, candidates)
    assert decision.source == "clef" and decision.chosen["id"] == "recon:192.168.1.10"
    assert decision.top_prob == pytest.approx(0.91)


def test_low_confidence_low_stakes_takes_the_deterministic_pick(tmp_path):
    llm = StubLLM({"id": "recon:192.168.1.10", "why": "x"})
    ctx, policy, decider = decider_for(tmp_path, StubClef(answers={
        "next_effect": {"type": "choice", "choice": "persist:192.168.1.10:auto",
                        "probabilities": {"persist:192.168.1.10:auto": 0.4,
                                          "recon:192.168.1.10": 0.35,
                                          "hold": 0.25}}}), llm=llm)
    candidates = [make_candidate("recon", {"ip": "192.168.1.10"}, 30, "scan it"),
                  make_candidate("persist", {"ip": "192.168.1.10", "family": "auto"},
                                 20, "plant it")]
    decision = decider.next_effect(1, {}, candidates)
    assert decision.source == "deterministic"
    assert decision.chosen["id"] == "recon:192.168.1.10"
    assert llm.calls == [], "a low-stakes call must not spend an LLM round trip"


def test_low_confidence_high_stakes_uses_the_arbiter(tmp_path):
    llm = StubLLM({"id": "recon:192.168.1.11", "why": "blue is watching"})
    ctx, policy, decider = decider_for(tmp_path, StubClef(answers={
        "next_effect": {"type": "choice", "choice": "takedown:192.168.1.10:nginx",
                        "probabilities": {"takedown:192.168.1.10:nginx": 0.41,
                                          "recon:192.168.1.11": 0.33,
                                          "hold": 0.26}}}), llm=llm)
    candidates = [make_candidate("takedown",
                                 {"ip": "192.168.1.10", "service": "nginx",
                                  "mode": "stop"}, 59, "break it", high_stakes=True),
                  make_candidate("recon", {"ip": "192.168.1.11"}, 15, "scan it")]
    decision = decider.next_effect(1, {}, candidates)
    assert decision.source == "llm_arbiter"
    assert decision.chosen["id"] == "recon:192.168.1.11"
    assert llm.calls and llm.calls[0]["options"] == ["takedown:192.168.1.10:nginx",
                                                     "recon:192.168.1.11"]


def test_arbiter_cannot_invent_an_option(tmp_path):
    llm = StubLLM({"id": "launch_missiles:1.2.3.4", "why": "fun"})
    ctx, policy, decider = decider_for(tmp_path, StubClef(answers={
        "next_effect": {"type": "choice", "choice": "takedown:192.168.1.10:nginx",
                        "probabilities": {"takedown:192.168.1.10:nginx": 0.41,
                                          "hold": 0.59}}}), llm=llm)
    candidates = [make_candidate("takedown",
                                 {"ip": "192.168.1.10", "service": "nginx",
                                  "mode": "stop"}, 59, "break it", high_stakes=True)]
    decision = decider.next_effect(1, {}, candidates)
    assert decision.chosen["id"] == "takedown:192.168.1.10:nginx"
    assert decision.source == "deterministic"
    assert any(e["kind"] == "arbiter_rejected" for e in ctx.log.read())


def test_clef_down_degrades_to_the_deterministic_pick(tmp_path):
    class DownClef(StubClef):
        def ask(self, state, questions):
            raise ClefError("connection refused")

    ctx, policy, decider = decider_for(tmp_path, DownClef())
    candidates = [make_candidate("recon", {"ip": "192.168.1.10"}, 15, "scan it"),
                  make_candidate("persist", {"ip": "192.168.1.10", "family": "auto"},
                                 45, "plant it")]
    decision = decider.next_effect(1, {}, candidates)
    assert decision.source == "deterministic"
    assert decision.chosen is not None                 # it still ACTS
    assert decision.chosen["id"] == candidates[0]["id"]
    assert candidates[0]["id"] in (decision.note or "")


def test_clef_naming_a_non_candidate_falls_back(tmp_path):
    ctx, policy, decider = decider_for(tmp_path, StubClef(answers={
        "next_effect": {"type": "choice", "choice": "not_a_real_option",
                        "probabilities": {"not_a_real_option": 0.99}}}))
    candidates = [make_candidate("recon", {"ip": "192.168.1.10"}, 15, "scan it")]
    decision = decider.next_effect(1, {}, candidates)
    assert decision.source == "deterministic"
    assert "not a candidate" in (decision.note or "")


def test_hold_is_a_legitimate_answer(tmp_path):
    ctx, policy, decider = decider_for(tmp_path, StubClef(answers={
        "next_effect": {"type": "choice", "choice": "hold",
                        "probabilities": {"hold": 0.8, "recon:192.168.1.10": 0.2}}}))
    candidates = [make_candidate("recon", {"ip": "192.168.1.10"}, 15, "scan it")]
    decision = decider.next_effect(1, {}, candidates)
    assert decision.chosen is None
    assert decision.source == "clef"


def test_no_candidates_is_a_clean_hold(tmp_path):
    ctx, policy, decider = decider_for(tmp_path, StubClef())
    decision = decider.next_effect(1, {}, [])
    assert decision.source == "deterministic" and decision.chosen is None
    assert decision.answer == "hold"


# --- the smaller questions ----------------------------------------------------

def test_pressure_count_uses_the_expected_value_indices():
    answer = {"type": "score", "score": 1.24,
              "probabilities": {"0": 0.3, "1": 0.4, "2": 0.2, "3": 0.1},
              "legend": {"0": "zero", "1": "one", "2": "two", "3": "three"}}
    expected, levels = decide_mod.score_of(answer)
    assert expected == pytest.approx(1.1)
    assert levels[3] == 0.1
    assert top_probability(answer) == pytest.approx(0.4)


def test_pressure_count_below_theta_falls_back(tmp_path):
    ctx, policy, decider = decider_for(tmp_path, StubClef(answers={
        "pressure_count": {"type": "score", "score": 2.9,
                           "probabilities": {"0": 0.3, "1": 0.3, "2": 0.2, "3": 0.2}}}))
    decision = decider.pressure_count({}, default=1)
    assert decision.answer == 1 and decision.source == "deterministic"
    assert any(e["kind"] == "low_confidence" for e in ctx.log.read())


def test_noul_uses_the_yes_probability(tmp_path):
    ctx, policy, decider = decider_for(tmp_path, StubClef(answers={
        "escalate_now": {"type": "noul", "noul": 0.82}}))
    decision = decider.noul("escalate_now", {}, "escalate?")
    assert decision.answer is True and decision.source == "clef"
    ctx2, _, decider2 = decider_for(tmp_path / "b", StubClef(answers={
        "escalate_now": {"type": "noul", "noul": 0.55}}))
    assert decider2.noul("escalate_now", {}, "escalate?").source == "deterministic"


def test_per_question_thresholds_override(tmp_path):
    cfg = {"clef": {"theta": 0.6, "theta_by_question": {"next_effect": 0.9}}}
    ctx, policy, decider = decider_for(tmp_path, StubClef(answers={
        "next_effect": {"type": "choice", "choice": "recon:192.168.1.10",
                        "probabilities": {"recon:192.168.1.10": 0.75, "hold": 0.25}}}),
        cfg_over=cfg)
    candidates = [make_candidate("recon", {"ip": "192.168.1.10"}, 15, "scan it")]
    decision = decider.next_effect(1, {}, candidates)
    assert decision.theta == 0.9
    assert decision.source == "deterministic"      # 0.75 < 0.9


# --- command intent -----------------------------------------------------------

def test_command_intent_keeps_the_deterministic_floor(tmp_path):
    ctx, policy, decider = decider_for(tmp_path, StubClef(answers={
        "command_intent": {"type": "choice", "choice": "benign",
                           "probabilities": {"benign": 0.9, "takedown": 0.1}}}))
    decision = decider.classify_command("systemctl stop nginx", "linux")
    assert decision.answer == "takedown"
    assert "kept deterministic" in (decision.note or "")


def test_command_intent_takes_the_model_when_it_is_not_softer(tmp_path):
    ctx, policy, decider = decider_for(tmp_path, StubClef(answers={
        "command_intent": {"type": "choice", "choice": "persistence",
                           "probabilities": {"persistence": 0.8, "recon": 0.2}}}))
    assert decider.classify_command("echo x >> ~/.bashrc", "linux").answer == \
        "persistence"
    assert keyword_intent("systemctl stop nginx") == "takedown"
    assert keyword_intent("id") == "recon"
    assert keyword_intent("echo hi") == "benign"


def test_step_verified_defaults_to_the_exit_status(tmp_path):
    ctx, policy, decider = decider_for(tmp_path, StubClef(answers={
        "step_verified": {"type": "noul", "noul": 0.9}}))
    assert decider.step_verified({}, "id", 0, "uid=0").answer is True
    ctx2, _, decider2 = decider_for(tmp_path / "b", StubClef(answers={
        "step_verified": {"type": "noul", "noul": 0.9}}))
    assert decider2.step_verified({}, "id", 1, "").answer is True   # model said yes
    # and the deterministic floor is the exit code
    assert decide_mod.noul_of({"noul": 0.9})[0] is True


# --- the client ---------------------------------------------------------------

def test_clef_client_enforces_the_batch_budget():
    from tlaloc.decide import ClefClient
    cfg = {"clef": {"enabled": True, "base_url": "http://x", "max_request_tokens": 64,
                    "timeout_sec": 1}}
    client = ClefClient(cfg, poster=lambda url, body, timeout: {"answers": {}})
    with pytest.raises(ClefError) as excinfo:
        client.ask({"big": "y" * 2000}, {"q": {"type": "noul", "instructions": "?"}})
    assert "batch budget" in str(excinfo.value)


def test_clef_client_cools_down_after_repeated_failures():
    from tlaloc.decide import ClefClient
    calls = {"n": 0}

    def poster(url, body, timeout):
        calls["n"] += 1
        raise RuntimeError("connection refused")

    cfg = {"clef": {"enabled": True, "base_url": "http://x", "max_request_tokens": 4096,
                    "failure_threshold": 2, "cooldown_sec": 60}}
    client = ClefClient(cfg, poster=poster)
    for _ in range(2):
        with pytest.raises(ClefError):
            client.ask({"a": 1}, {"q": {"type": "noul", "instructions": "?"}})
    assert client.available() is False           # cooling down, no more calls
    with pytest.raises(ClefError):
        client.ask({"a": 1}, {"q": {"type": "noul", "instructions": "?"}})
    assert calls["n"] == 2


def _wide_board(ctx, n=6, reason_pad=0):
    # 12 boxes with footholds + one eviction candidate per box
    for team_octet in (1, 2):
        for host in range(1, n + 1):
            ctx.world.add_foothold(f"192.168.{team_octet}.{host}0", "ubuntu",
                                   "boxpw-1", "root")
    pad = "x" * reason_pad
    return [make_candidate("reestablish", {"ip": f"192.168.{t}.{h}0"},
                           90 - h, f"evicted box {h}{pad}")
            for t in (1, 2) for h in range(1, n + 1)]


def test_compose_sends_the_whole_state_to_the_16k_endpoint(tmp_path):
    """At the 16k batch a wide board goes WHOLE — no compaction, nothing off the digest."""
    from tlaloc.decide import tokens_needed
    ctx, policy, decider = decider_for(tmp_path, StubClef())
    candidates = _wide_board(ctx)
    recent = [f"cycle {i} stop_mask web01 nginx ok" for i in range(40)]
    assert ctx.cfg["clef"]["max_request_tokens"] == 16384        # the live default
    digest, questions, meta = decider.compose(ctx, candidates,
                                              recent=list(recent[-24:]), cycle=1)
    assert tokens_needed(digest, questions, ratio=decider.ratio()) <= 16384
    # 8 actions (A..H) + HOLD: the full option list, un-compacted
    assert len(questions) == 9 and "HOLD" in questions
    assert "_dropped" not in digest, f"the digest was trimmed: {digest.get('_dropped')}"
    assert len(digest["teams"]) >= 2
    assert digest["rules_of_engagement"]["allowed_mechanisms"]
    assert len(digest["recent_events"]) == 6                            # a handful of events
    kinds = [e["kind"] for e in ctx.log.read()]
    assert "clef_bundle_compact" not in kinds
    assert "clef_bundle_over_budget" not in kinds


def test_compose_still_compacts_against_a_small_batch(tmp_path):
    """A 1k endpoint (a smaller clef, or another model) compacts the bundle and shrinks the
    digest rather than composing a request it would reject."""
    from tlaloc.decide import tokens_needed
    ctx, policy, decider = decider_for(tmp_path, StubClef())
    candidates = _wide_board(ctx, reason_pad=140)                # fat reasons
    ctx.cfg["clef"]["max_request_tokens"] = 2000
    digest, questions, meta = decider.compose(ctx, candidates, recent=["x"] * 6, cycle=1)
    assert tokens_needed(digest, questions, ratio=decider.ratio()) <= 2000
    assert len(questions) <= 5                                  # 4 compacted actions + HOLD
    assert "clef_bundle_compact" in [e["kind"] for e in ctx.log.read()]


def test_a_batch_too_small_for_any_bundle_is_flagged_not_truncated(tmp_path):
    """Below the floor the request is FLAGGED and the client refuses it, so the chain degrades
    deterministically — a shortened question, or a digest that lost the ROE, is banned.
    Measured floor on this board: ~577 tokens."""
    from tlaloc.decide import tokens_needed
    ctx, policy, decider = decider_for(tmp_path, StubClef())
    candidates = _wide_board(ctx, reason_pad=140)
    ctx.cfg["clef"]["max_request_tokens"] = 512
    digest, questions, meta = decider.compose(ctx, candidates, recent=["x"] * 6, cycle=1)
    assert "clef_bundle_over_budget" in [e["kind"] for e in ctx.log.read()]
    assert digest.get("_over_budget") is True
    # the digest gave ground (it may be minimal) but never lost the ROE
    assert set(digest.get("rules_of_engagement") or {}) >= {"window", "allowed_mechanisms"}
    assert tokens_needed(digest, questions, ratio=decider.ratio()) > 512


def test_an_over_budget_rejection_is_learned_and_retried_once(tmp_path):
    # The endpoint's own token count beats our estimate: learn it, shrink, retry.
    calls = {"n": 0}

    def poster(url, body, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError('clef HTTP 500: {"error":{"message":"input (960 tokens) '
                               'is too large to process"}}')
        return {"answers": {"next_effect": {"type": "choice", "choice": "hold",
                                            "probabilities": {"hold": 0.9}}},
                "usage": {}}

    ctx, policy, decider = decider_for(tmp_path, StubClef(), cfg_over={"clef": {
        "enabled": True, "base_url": "http://x", "max_request_tokens": 16384,
        "timeout_sec": 1}})
    decider.clef = decide_mod.ClefClient(ctx.cfg, poster=poster)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root")
    candidates = [make_candidate("reestablish", {"ip": f"192.168.{t}.{h}0"},
                                 50, f"evicted {t}-{h}")
                  for t in (1, 2) for h in range(1, 7)]
    decision = decider.next_effect(1, None, candidates, ctx=ctx, recent=["x"] * 6)
    assert calls["n"] == 2, "one rejection, one retry — never a loop"
    assert decider.clef.learned_ratio is not None
    assert decision.source == "clef"
    assert any(e["kind"] == "clef_bundle_compact" for e in ctx.log.read())


def test_tokens_needed_tracks_the_bundle_size():
    small = decide_mod.tokens_needed({"a": 1}, {"q": {"type": "noul",
                                                      "instructions": "x"}})
    big = decide_mod.tokens_needed({"a": "y" * 400}, {"q": {"type": "noul",
                                                            "instructions": "x"}})
    assert small < big


def test_pressure_count_low_confidence_is_not_max(tmp_path):
    ctx, policy, decider = decider_for(tmp_path, StubClef(answers={
        "pressure_count": {"type": "score", "score": 2.9,
                           "probabilities": {"0": 0.3, "1": 0.3, "2": 0.2, "3": 0.2}}}))
    policy.red["budget"]["extra_pressure_max"] = 3
    decision = decider.pressure_count({})
    assert decision.answer == 0 and decision.source == "deterministic"
    # the model's answer is capped by the same ROE bound
    policy.red["budget"]["extra_pressure_max"] = 2
    ctx2, _, decider2 = decider_for(tmp_path / "cap", StubClef(answers={
        "pressure_count": {"type": "score", "score": 3.0,
                           "probabilities": {"3": 0.9}}}))
    policy2 = decider2.policy
    policy2.red["budget"]["extra_pressure_max"] = 2
    assert decider2.pressure_count({}).answer == 2


def _noul_actions():
    return [
        make_candidate("takedown", {"ip": "192.168.1.10", "service": "nginx",
                                    "mode": "stop"}, 60, "standing web service"),
        make_candidate("persist", {"ip": "192.168.1.10", "family": "cron"},
                       45, "keep access after eviction"),
    ]


def _noul_answers(a, b, hold):
    return {"A": {"type": "noul", "noul": a},
            "B": {"type": "noul", "noul": b},
            "HOLD": {"type": "noul", "noul": hold}}


def test_next_effect_noul_selects_max_yes(tmp_path):
    ctx, policy, decider = decider_for(tmp_path, StubClef(
        answers=_noul_answers(0.4, 0.8, 0.1)))
    decision = decider.next_effect(1, None, _noul_actions(), ctx=ctx)
    assert decision.source == "clef"
    assert decision.chosen["id"] == "persist:192.168.1.10:cron"
    assert decision.meta["p_yes"]["B"] == 0.8
    assert decision.meta["shape"] == "action_noul_v1"


def test_next_effect_noul_uses_p_yes_not_max(tmp_path):
    ctx, policy, decider = decider_for(tmp_path, StubClef(
        answers=_noul_answers(0.3, 0.2, 0.1)))
    decision = decider.next_effect(1, None, _noul_actions(), ctx=ctx)
    assert decision.source == "deterministic"
    assert decision.top_prob == 0.3          # not max(0.3, 1-0.3) == 0.7
    assert decision.meta["p_yes"]["A"] == 0.3


def test_next_effect_noul_hold_wins(tmp_path):
    ctx, policy, decider = decider_for(tmp_path, StubClef(
        answers=_noul_answers(0.8, 0.2, 0.9)))
    decision = decider.next_effect(1, None, _noul_actions(), ctx=ctx)
    assert decision.source == "clef" and decision.chosen is None
    assert decision.answer == "HOLD"
    assert decision.meta["p_hold"] == 0.9


def test_next_effect_noul_low_confidence_falls_back(tmp_path):
    ctx, policy, decider = decider_for(tmp_path, StubClef(
        answers=_noul_answers(0.2, 0.1, 0.1)))
    decision = decider.next_effect(1, None, _noul_actions(), ctx=ctx)
    assert decision.source == "deterministic"
    assert decision.chosen["id"] == _noul_actions()[0]["id"]


def test_next_effect_noul_never_invents(tmp_path):
    ctx, policy, decider = decider_for(tmp_path, StubClef(
        answers={"A": {"type": "noul", "noul": 0.1},
                 "Z": {"type": "noul", "noul": 0.99},
                 "HOLD": {"type": "noul", "noul": 0.1}}))
    decision = decider.next_effect(1, None, _noul_actions(), ctx=ctx)
    assert decision.source == "deterministic"
    assert decision.chosen["id"] == _noul_actions()[0]["id"]


def test_noul_option_keys_are_short_and_unique(tmp_path):
    ctx, policy = decider_for(tmp_path, StubClef())[:2]
    questions, meta = policy.question_bundle(None, _noul_actions(), ctx,
                                             action_noul=True)
    keys = set(meta["option_keys"])
    assert keys == {"A", "B"} and "HOLD" in questions
    assert all(len(key) == 1 for key in keys)
    assert len(set(meta["option_keys"].values())) == 2


def test_expanded_state_has_no_secrets_and_fits_budget(tmp_path):
    ctx, policy = decider_for(tmp_path, StubClef())[:2]
    state = policy.clef_state(ctx, recent=["x"], compact=False)
    blob = json.dumps(state).lower()
    assert "password" not in blob and "secret" not in blob and "credlist" not in blob
    from tlaloc.decide import tokens_needed
    assert tokens_needed(state, policy.question_bundle(None, _noul_actions(), ctx,
                                                       action_noul=True)[0]) < 16384
    compact = policy.clef_state(ctx, recent=["x"], compact=True)
    assert compact["teams"]
