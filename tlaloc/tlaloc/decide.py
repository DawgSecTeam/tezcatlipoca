"""The decision layer: clef answers typed questions; everything else is a bounded fallback."""

import json
import re
import time
from dataclasses import dataclass, field

from .errors import ClefError, LLMError, PolicyError
from .state import digest as digest_mod

# Each question replaces a named heuristic (below); that is its admission test.
CATALOG = {
    "next_effect": {"type": "choice", "replaces": "3 override gates + fallback ladder"},
    "pressure_count": {"type": "score", "replaces": "press_impacts_per_cycle heuristics"},
    "escalate_now": {"type": "noul", "replaces": "restore-streak escalation rules"},
    "command_intent": {"type": "choice", "replaces": "_IMPACTY_WORDS substring sniff"},
    "step_verified": {"type": "noul", "replaces": "ok = any(rc == 0)"},
    "blue_hunting": {"type": "noul", "replaces": "ad-hoc tempo/restore heuristics"},
}

DEFAULT_THETA = 0.60
# Caps digest cost even when the endpoint's batch is bigger.
DIGEST_BUDGET_CEILING = 8192
INTENTS = ("takedown", "recon", "persistence", "benign")


class ClefClient:
    """POST /v1/systemone — typed questions in, probabilities out. requests is
    lazy: a bare red01 may not have it."""

    def __init__(self, cfg, poster=None, now=None):
        clef = cfg.get("clef") or {}
        self.enabled = bool(clef.get("enabled", True))
        self.base_url = (clef.get("base_url") or "").rstrip("/")
        self.path = clef.get("path") or "/v1/systemone"
        self.model = clef.get("model") or "clef-flash"
        self.timeout = float(clef.get("timeout_sec", 5))
        self.max_request_tokens = int(clef.get("max_request_tokens", 16384))
        self.tokens_per_char = float(clef.get("tokens_per_char",
                                              digest_mod.TOKENS_PER_CHAR))
        # A "too large" rejection reports the endpoint's real token count; keep
        # the ratio it implies so that size is never sent again.
        self.learned_ratio = None
        self._poster = poster
        self._now = now or time.time
        self.calls = 0
        self.failures = 0
        self.last_error = None
        self.last_input_tokens = None
        self.disabled_until = 0.0
        self.failure_threshold = int(clef.get("failure_threshold", 3))
        self.cooldown_sec = float(clef.get("cooldown_sec", 120))

    def available(self):
        if not self.enabled or not self.base_url:
            return False
        return self._now() >= self.disabled_until

    def _note_failure(self, exc):
        self.failures += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        if self.failures >= self.failure_threshold:
            self.disabled_until = self._now() + self.cooldown_sec
            self.failures = 0

    def _note_success(self):
        self.failures = 0
        self.last_error = None

    def ask(self, state, questions):
        if not self.available():
            raise ClefError("clef unavailable (disabled, unconfigured, or cooling "
                            f"down until {self.disabled_until:.0f})")
        body = {"state": state, "questions": questions}
        ratio = max(self.tokens_per_char, self.learned_ratio or 0.0)
        estimate = tokens_needed(state, questions, ratio=ratio)
        if estimate > self.max_request_tokens:
            # caller bug, and the endpoint 500s anyway: refuse before sending
            raise ClefError(f"request is ~{int(estimate)} tokens at "
                            f"{ratio:.2f} tokens/char, over the "
                            f"{self.max_request_tokens}-token batch budget — the "
                            "digest/question bundle must be trimmed upstream")
        try:
            payload = self._post(body)
        except Exception as exc:                     # requests/urllib/JSON errors
            self._note_failure(exc)
            text = str(exc)
            real = _too_large_tokens(text)
            if real:
                chars = max(1, len(json.dumps(body, separators=(",", ":"), default=str)))
                self.learned_ratio = min(1.0, (real + 16) / chars)
                raise ClefError(f"over_budget: {text[:200]}") from exc
            raise ClefError(text) from exc
        if not isinstance(payload, dict) or "answers" not in payload:
            self._note_failure(ValueError("malformed response"))
            raise ClefError(f"malformed clef response: {str(payload)[:200]}")
        self.calls += 1
        self.last_input_tokens = (payload.get("usage") or {}).get("input_tokens")
        self._note_success()
        return payload

    def _post(self, body):
        if self._poster is not None:
            return self._poster(f"{self.base_url}{self.path}", body, self.timeout)
        import requests                                 # lazy
        response = requests.post(f"{self.base_url}{self.path}", json=body,
                                 timeout=self.timeout)
        if response.status_code != 200:
            raise ClefError(f"clef HTTP {response.status_code}: "
                            f"{response.text[:200]}")
        return response.json()


def _too_large_tokens(text):
    match = re.search(r"input \((\d+) tokens\) is too large", text or "")
    if match:
        return int(match.group(1))
    return None


class StubClef:
    """Recorded responder for the replay tests and --dry-run smoke chains."""

    def __init__(self, answers=None, responder=None):
        self.answers = dict(answers or {})
        self.responder = responder
        self.calls = 0
        self.enabled = True
        self.max_request_tokens = 16384
        self.last_input_tokens = None

    def available(self):
        return True

    def ask(self, state, questions):
        self.calls += 1
        if self.responder is not None:
            return self.responder(state, questions)
        return {"answers": {qid: self.answers.get(qid, {"type": _type_of(q)})
                            for qid, q in questions.items()},
                "usage": {"input_tokens": 0, "output_tokens": 0}}


def _type_of(question):
    return (question or {}).get("type", "noul")


def top_probability(answer):
    """The gate's number. noul answers carry no probabilities map — checking the
    map first reads every yes/no as 0.0-confidence."""
    if answer.get("type") == "noul" or ("noul" in answer and not answer.get("probabilities")):
        yes = float(answer.get("noul", 0.0) or 0.0)
        return max(yes, 1.0 - yes)
    probs = _probabilities(answer)
    if not probs:
        return 0.0
    return max(probs.values())


def choice_of(answer):
    if answer.get("choice"):
        return answer["choice"]
    probs = _probabilities(answer)
    if not probs:
        return None
    return max(probs, key=lambda k: probs[k])


def score_of(answer):
    probs = _probabilities(answer)
    if not probs:
        value = answer.get("score")
        return float(value or 0.0), {}
    levels = {}
    for key, prob in probs.items():
        try:
            levels[int(key)] = float(prob)
        except (TypeError, ValueError):
            continue
    expected = sum(level * prob for level, prob in levels.items())
    return expected, levels


def noul_of(answer):
    yes = float(answer.get("noul", 0.0) or 0.0)
    return yes >= 0.5, yes


def _probabilities(answer):
    raw = (answer or {}).get("probabilities") or {}
    out = {}
    for key, value in raw.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


@dataclass
class Decision:
    question: str
    answer: object
    probabilities: dict = field(default_factory=dict)
    top_prob: float = 0.0
    theta: float = DEFAULT_THETA
    source: str = "deterministic"      # clef | llm_arbiter | deterministic
    chosen: dict = None
    bundle: dict = None
    note: str = None
    meta: dict = field(default_factory=dict)

    def as_log(self):
        return {"question": self.question, "answer": self.answer,
                "probabilities": self.probabilities,
                "top_prob": round(self.top_prob, 4), "theta": self.theta,
                "source": self.source,
                "chosen": (self.chosen or {}).get("id") if self.chosen else None,
                "note": self.note, "meta": self.meta}


class Decider:
    def __init__(self, cfg, world, log, clef=None, llm=None, policy=None):
        self.cfg = cfg
        self.world = world
        self.log = log
        self.clef = clef if clef is not None else ClefClient(cfg)
        self.llm = llm
        self.policy = policy

    def theta(self, question):
        clef = self.cfg.get("clef") or {}
        overrides = clef.get("theta_by_question") or {}
        try:
            return float(overrides.get(question, clef.get("theta", DEFAULT_THETA)))
        except (TypeError, ValueError):
            return DEFAULT_THETA

    def high_stakes(self, candidates, chosen_id=None):
        """Configured effect list OR explicit flag — hand-built candidates included."""
        effects = tuple((self.cfg.get("clef") or {}).get("high_stakes_effects")
                        or ("takedown", "lockout", "rotate_credlist"))
        def risky(candidate):
            return bool(candidate.get("high_stakes")
                        or candidate.get("effect") in effects)
        if chosen_id is None:
            return any(risky(c) for c in candidates)
        hit = next((c for c in candidates if c.get("id") == chosen_id), None)
        return bool(hit and risky(hit))

    def ratio(self):
        return max(float(getattr(self.clef, "tokens_per_char",
                                 digest_mod.TOKENS_PER_CHAR) or 0.0),
                   float(getattr(self.clef, "learned_ratio", None) or 0.0))

    def compose(self, ctx, candidates, recent=None, *, cycle=0,
                include_escalation=False, include_blue_check=False, extra_shrink=0):
        """Questions first; the digest gets the remainder of the batch budget."""
        ceiling = int((self.cfg.get("clef") or {}).get("max_request_tokens", 16384))
        ratio = self.ratio()
        digest = questions = meta = None
        # (compact?, shrink) cheapest first: drop bundle quality only when the batch demands it
        attempts = [(False, 0), (True, 0), (True, 60), (True, 120)]
        if extra_shrink:
            attempts = [(True, extra_shrink + shrink) for _, shrink in attempts]
        for compact, shrink in attempts:
            # compact also trims the option list: option text eats a small batch
            # include_pressure=False: the loop asks pressure_count as its own
            # tiny call; bundling it would spend the batch twice.
            questions, meta = self.policy.question_bundle(
                None, candidates, ctx, cycle=cycle, compact=compact,
                max_options=(3 if extra_shrink else 4) if compact else 8,
                include_escalation=include_escalation,
                include_blue_check=include_blue_check, include_pressure=False,
                action_noul=True)
            overhead = tokens_needed({}, questions, ratio=ratio) + 24
            # a digest is a decision input, not a data dump; the low floor lets
            # a small batch shrink it until a compact attempt fits
            budget = int(min(max(80, ceiling - overhead - shrink),
                             DIGEST_BUDGET_CEILING))
            digest = self.policy.clef_state(ctx, recent=recent, compact=compact)
            if tokens_needed(digest, questions, ratio=ratio) <= ceiling:
                if compact or extra_shrink:
                    self.log.emit("clef_bundle_compact", cycle=cycle,
                                  tokens=tokens_needed(digest, questions, ratio=ratio),
                                  ceiling=ceiling, tokens_per_char=round(ratio, 3))
                return digest, questions, meta
        # Nothing fit: return the smallest bundle; the client's over-budget
        # refusal then degrades the chain deterministically — never a silent
        # truncated send.
        if isinstance(digest, dict):
            digest["_over_budget"] = True
        self.log.emit("clef_bundle_over_budget", cycle=cycle,
                      tokens=tokens_needed(digest, questions, ratio=ratio),
                      ceiling=ceiling, tokens_per_char=round(ratio, 3),
                      state_chars=len(digest_mod.dumps(digest)),
                      questions_chars=len(digest_mod.dumps(questions)))
        return digest, questions, meta

    def _ask(self, cycle, digest, questions, bundle, *, ctx=None, candidates=None,
             recent=None, include_escalation=False, include_blue_check=False):
        """Ask clef; on an over-budget rejection recompute once and retry — never a loop."""
        out = {"digest": digest, "questions": questions, "bundle": bundle,
               "payload": None, "error": None}
        try:
            out["payload"] = self.clef.ask(digest, questions)
            return out
        except (ClefError, AttributeError) as exc:
            text = str(exc)
            out["error"] = exc
            if ctx is None or not ("over_budget" in text or "too large" in text):
                return out
        digest, questions, meta = self.compose(
            ctx, candidates, recent, cycle=cycle, extra_shrink=200,
            include_escalation=include_escalation,
            include_blue_check=include_blue_check)
        out.update({"digest": digest, "questions": questions,
                    "bundle": {"state": digest, "questions": questions},
                    "meta": meta, "error": None})
        try:
            out["payload"] = self.clef.ask(digest, questions)
        except (ClefError, AttributeError) as exc:
            out["error"] = exc
        return out

    def next_effect(self, cycle, digest, candidates, *, ctx=None, recent=None,
                    include_escalation=False, include_blue_check=False):
        """Pick one candidate from the bounded list; new shape is per-action noul."""
        if not candidates:
            return self._log(cycle, Decision("next_effect", "hold",
                                             source="deterministic",
                                             note="no candidates this cycle",
                                             meta={"options": []}))
        if ctx is not None:
            digest, questions, meta = self.compose(
                ctx, candidates, recent, cycle=cycle,
                include_escalation=include_escalation,
                include_blue_check=include_blue_check)
        else:
            questions, meta = self.policy.question_bundle(
                digest, candidates, None, cycle=cycle,
                include_escalation=include_escalation,
                include_blue_check=include_blue_check)
        bundle = {"state": digest, "questions": questions}
        theta = self.theta("next_effect")
        asked = self._ask(cycle, digest, questions, bundle, ctx=ctx,
                          candidates=candidates, recent=recent,
                          include_escalation=include_escalation,
                          include_blue_check=include_blue_check)
        bundle, payload = asked["bundle"], asked["payload"]
        meta = {**meta, **(asked.get("meta") or {})}
        if payload is None:
            chosen = candidates[0]
            return self._log(cycle, Decision(
                "next_effect", chosen["id"], source="deterministic", bundle=bundle,
                chosen=chosen, theta=theta,
                note=f"clef unavailable ({asked['error']}) — deterministic pick "
                     f"{chosen['id']}",
                meta={**meta, "fallback": chosen["id"],
                      "chosen_key": None, "candidate_count": len(candidates),
                      "high_stakes": self.high_stakes(candidates, chosen["id"])}))
        answers = payload.get("answers") or {}
        if (answers.get("next_effect", {}).get("type") == "choice"
                or (not any(key in answers for key in ("A", "HOLD"))
                    and answers.get("next_effect"))):
            return self._legacy_choice(cycle, bundle, candidates, answers, meta, theta)

        keys = dict(meta.get("option_keys") or {})
        p_yes = {}
        for key, cid in keys.items():
            if self._candidate_for(candidates, cid) is not None:
                p_yes[key] = round(float(noul_of(answers.get(key) or {})[1]), 4)
        hold_answer = answers.get("HOLD") or {}
        p_hold = round(float(hold_answer.get("noul", 0.0) or 0.0), 4)
        probabilities = dict(p_yes)
        probabilities["HOLD"] = p_hold
        top_key = max(p_yes, key=p_yes.get) if p_yes else None
        top_action = p_yes.get(top_key, 0.0) if top_key else 0.0
        top_id = keys.get(top_key) if top_key else None
        common = {"shape": meta.get("shape", "action_noul_v1"),
                  "p_yes": p_yes, "p_hold": p_hold,
                  "option_keys": keys, "candidate_count": len(candidates),
                  "input_tokens": getattr(self.clef, "last_input_tokens", None),
                  "usage": {"input_tokens": getattr(self.clef, "last_input_tokens", None)},
                  "compact": bool(meta.get("compact")),
                  "over_budget": bool((bundle.get("state") or {}).get("_over_budget"))}
        if p_hold >= theta and p_hold >= top_action:
            return self._log(cycle, Decision(
                "next_effect", "HOLD", probabilities, p_hold, theta, "clef", None,
                bundle, note=f"hold p={p_hold:.2f} >= theta {theta:.2f}",
                meta={**common, "chosen_key": "HOLD", "chosen_candidate": None,
                      "high_stakes": False}))
        if top_key and top_action >= theta:
            chosen = self._candidate_for(candidates, top_id)
            return self._log(cycle, Decision(
                "next_effect", top_key, probabilities, top_action, theta, "clef",
                chosen, bundle,
                note=f"action {top_key} p_yes={top_action:.2f} >= theta {theta:.2f}",
                meta={**common, "chosen_key": top_key, "chosen_candidate": top_id,
                      "high_stakes": self.high_stakes(candidates, top_id)}))
        note = (f"max action p_yes {top_action:.2f} and hold p={p_hold:.2f} "
                f"below theta {theta:.2f}")
        high_stakes = self.high_stakes(candidates, top_id)
        if high_stakes:
            arbitrated = self.arbitrate(candidates, bundle, note)
            if arbitrated is not None:
                chosen, why = arbitrated
                return self._log(cycle, Decision(
                    "next_effect", chosen["id"], probabilities, top_action, theta,
                    "llm_arbiter", chosen, bundle,
                    note=f"{note}; arbiter chose {chosen['id']}: {why}",
                    meta={**common, "chosen_key": None,
                          "chosen_candidate": chosen["id"],
                          "high_stakes": True}))
        chosen = candidates[0]
        return self._log(cycle, Decision(
            "next_effect", chosen["id"], probabilities, top_action, theta,
            "deterministic", chosen, bundle,
            note=f"{note} — deterministic pick {chosen['id']}",
            meta={**common, "chosen_key": None, "chosen_candidate": chosen["id"],
                  "high_stakes": high_stakes}))

    @staticmethod
    def _candidate_for(candidates, cid):
        return next((c for c in candidates if c["id"] == cid), None)

    def _legacy_choice(self, cycle, bundle, candidates, answers, meta, theta):
        """Old recorded choice bundles remain replayable; live bundles use action_noul_v1."""
        answer = answers.get("next_effect") or {}
        probs = _probabilities(answer)
        prob = top_probability(answer)
        picked = choice_of(answer)
        if prob >= theta and picked == "hold":
            return self._log(cycle, Decision(
                "next_effect", "hold", probs, prob, theta, "clef", None, bundle,
                note="clef chose to hold, above theta",
                meta={**meta, "shape": "choice_legacy", "chosen_key": "hold"}))
        if prob >= theta and picked:
            chosen = next((c for c in candidates if c["id"] == picked), None)
            if chosen is not None:
                return self._log(cycle, Decision("next_effect", picked, probs, prob,
                                                 theta, "clef", chosen, bundle,
                                                 meta={**meta, "shape": "choice_legacy"}))
            note = f"clef chose {picked!r}, which is not a candidate — falling back"
        else:
            note = (f"clef top probability {prob:.2f} < theta {theta:.2f}"
                    if picked else "clef returned no choice")
        if self.high_stakes(candidates, picked):
            arbitrated = self.arbitrate(candidates, bundle, note)
            if arbitrated is not None:
                chosen, why = arbitrated
                return self._log(cycle, Decision(
                    "next_effect", chosen["id"], probs, prob, theta, "llm_arbiter",
                    chosen, bundle,
                    note=f"{note}; arbiter chose {chosen['id']}: {why}",
                    meta={**meta, "shape": "choice_legacy"}))
        chosen = candidates[0]
        return self._log(cycle, Decision(
            "next_effect", chosen["id"], probs, prob, theta, "deterministic",
            chosen, bundle, note=f"{note} — deterministic pick {chosen['id']}",
            meta={**meta, "shape": "choice_legacy"}))

    def arbitrate(self, candidates, bundle, reason):
        """One bounded LLM call: choose from the candidate list, or decline."""
        if self.llm is None or not getattr(self.llm, "enabled", False):
            return None
        options = [{"id": c["id"], "effect": c["effect"], "reason": c["reason"],
                    "params": c["params"]} for c in candidates]
        try:
            reply = self.llm.choose(options, context={"reason": reason})
        except LLMError as exc:
            self.log.emit("arbiter_failed", reason=str(exc))
            return None
        if not reply:
            return None
        chosen = next((c for c in candidates if c["id"] == reply.get("id")), None)
        if chosen is None:
            self.log.emit("arbiter_rejected", reply=reply,
                          why="not a candidate id")
            return None
        return chosen, (reply.get("why") or "")[:200]

    # -- the smaller questions ----------------------------------------------

    def pressure_count(self, digest, *, default=None, cycle=None):
        """How many extra takedowns this cycle (a `score` answer)."""
        budget = int((self.policy.red.get("budget") or {}).get("extra_pressure_max", 1))
        if default is None:
            # An uncertain score must not become maximum pressure; baseline low-confidence
            # cycles fired the cap 73% of the time.
            default = 0
        questions = {"pressure_count": {
            "type": "score",
            "instructions": (f"Additional scored services to take down this cycle "
                             f"(cap {self.policy.max_down_now()} per team)?"),
            "criteria": ["zero", "one", "two", "three"]}}
        try:
            payload = self.clef.ask(digest, questions)
        except (ClefError, AttributeError) as exc:
            return self._log(cycle, Decision("pressure_count", default,
                                             source="deterministic",
                                             note=f"clef unavailable ({exc})"))
        answer = (payload.get("answers") or {}).get("pressure_count") or {}
        expected, levels = score_of(answer)
        prob = top_probability(answer)
        theta = self.theta("pressure_count")
        value = int(round(expected))
        source = "clef"
        if prob < theta:
            self.log.emit("low_confidence", question="pressure_count",
                          top_prob=round(prob, 3), theta=theta, expected=expected)
            value = default
            source = "deterministic"
        value = min(int(value), budget)
        return self._log(cycle, Decision(
            "pressure_count", value, _probabilities(answer), prob, theta, source,
            note=f"expected {expected:.2f} from levels {levels}"))

    def noul(self, question, digest, instructions, *, default=False, cycle=None):
        questions = {question: {"type": "noul", "instructions": instructions}}
        theta = self.theta(question)
        try:
            payload = self.clef.ask(digest, questions)
        except (ClefError, AttributeError) as exc:
            return self._log(cycle, Decision(question, default,
                                             source="deterministic",
                                             note=f"clef unavailable ({exc}) — "
                                                  f"default {default}"))
        answer = (payload.get("answers") or {}).get(question) or {}
        value, yes = noul_of(answer)
        prob = top_probability(answer)
        if prob < theta:
            self.log.emit("low_confidence", question=question, p_yes=yes,
                          top_prob=round(prob, 3), theta=theta, default=default)
            return self._log(cycle, Decision(question, default,
                                             {"no": 1 - yes, "yes": yes}, prob, theta,
                                             "deterministic",
                                             note=f"below theta — default {default}"))
        return self._log(cycle, Decision(question, value, {"no": 1 - yes, "yes": yes},
                                         prob, theta, "clef"))

    def classify_command(self, command, os_name="linux"):
        """command_intent: informs attribution, never a gate. The deterministic keyword read is the floor."""
        base = keyword_intent(command)
        digest = {"cmd": str(command)[:120], "os": os_name, "kw": base}
        questions = {"command_intent": {
            "type": "choice",
            "instructions": ("Classify this command's effect on the target. "
                             "takedown = degrades a scored service, recon = reads only, "
                             "persistence = plants/repairs access, benign = none of these."),
            "criteria": {
                "takedown": "stops/breaks/denies a scored service",
                "recon": "reads information only",
                "persistence": "plants or repairs access/artifacts",
                "benign": "none of the above",
            }}}
        decision = self._ask_choice("command_intent", digest, questions, default=base)
        intent = decision.answer
        if intent == "benign" and base in ("takedown", "persistence"):
            # the conservative union: the deterministic read wins when it sees
            # impact the model does not
            decision.note = (decision.note or "") + " (kept deterministic " \
                f"{base!r}: model said benign)"
            intent = base
            decision.answer = intent
        return decision

    def step_verified(self, digest, command, rc, output, *, default=None):
        """Whether an improvised one-shot actually did what it was for."""
        if default is None:
            default = rc == 0
        questions = {"step_verified": {
            "type": "noul",
            "instructions": ("Did this command achieve its stated goal? Judge from the "
                             "exit status and output only; a command that ran but "
                             "achieved nothing is NO."),
        }}
        state = {"cmd": str(command)[:120], "rc": rc, "out": str(output or "")[:200],
                 "digest_t": (digest or {}).get("t")}
        decision = self._ask_noul("step_verified", state, questions, default=default)
        return decision

    def _ask_choice(self, question, state, questions, default):
        theta = self.theta(question)
        try:
            payload = self.clef.ask(state, questions)
        except (ClefError, AttributeError) as exc:
            return Decision(question, default, source="deterministic",
                            note=f"clef unavailable ({exc}) — default {default}")
        answer = (payload.get("answers") or {}).get(question) or {}
        prob = top_probability(answer)
        if prob < theta:
            return Decision(question, default, _probabilities(answer), prob, theta,
                            "deterministic", note=f"below theta — default {default}")
        return Decision(question, choice_of(answer), _probabilities(answer), prob,
                        theta, "clef")

    def _ask_noul(self, question, state, questions, default):
        theta = self.theta(question)
        try:
            payload = self.clef.ask(state, questions)
        except (ClefError, AttributeError) as exc:
            return Decision(question, default, source="deterministic",
                            note=f"clef unavailable ({exc}) — default {default}")
        answer = (payload.get("answers") or {}).get(question) or {}
        value, yes = noul_of(answer)
        prob = top_probability(answer)
        if prob < theta:
            return Decision(question, default, {"no": 1 - yes, "yes": yes}, prob, theta,
                            "deterministic", note=f"below theta — default {default}")
        return Decision(question, value, {"no": 1 - yes, "yes": yes}, prob, theta, "clef")

    def _log(self, cycle, decision):
        """Log one decision's whole chain and return it."""
        if cycle is not None:
            record = decision.as_log()
            self.log.decision(cycle=cycle, question=record["question"],
                              bundle=decision.bundle,
                              answer=record["answer"],
                              probabilities=record["probabilities"],
                              theta=record["theta"], source=record["source"],
                              chosen=record["chosen"], ok=None,
                              note=record["note"], top_prob=record["top_prob"],
                              meta=record["meta"])
        return decision

    def record(self, cycle=None, decisions=(), *, executed=None):
        for decision in decisions:
            self._log(cycle, decision)
        if executed is not None:
            self.log.emit("decision_result", cycle=cycle, executed=executed)


# Word-boundary, not substring ("id" matched inside "hidden"); check order is
# the conservative one: impact first.
IMPACT_RE = re.compile(
    r"\b(stop|disable|mask|kill|lock|flush|sabotage|drain|deny|block|down)\b"
    r"|systemctl\s+(stop|mask|disable)|/active:no|-Recurse\b", re.IGNORECASE)
RECON_RE = re.compile(
    r"\b(nmap|netstat|whoami|id|ps|ls|cat|ss|find|ip|arp|systemctl\s+status"
    r"|get-service|get-nettcpconnection|get-process|ipconfig|net\s+user)\b",
    re.IGNORECASE)
PERSIST_RE = re.compile(
    r"\b(cron|crond|crontab|systemd|unit|authorized_keys|task|schtasks|register-scheduledtask"
    r"|run\b|wmi|profile\.d|rc\.local|bashrc|service)\b", re.IGNORECASE)


def keyword_intent(command):
    text = str(command or "")
    if IMPACT_RE.search(text):
        return "takedown"
    if PERSIST_RE.search(text):
        return "persistence"
    if RECON_RE.search(text):
        return "recon"
    return "benign"


def replay_answer(record):
    return {"type": "choice" if isinstance(record.get("answer"), str) else "noul",
            "choice": record.get("answer"),
            "probabilities": record.get("probabilities") or {}}


def tokens_needed(state, questions, ratio=None):
    from .state.digest import estimate_tokens
    return estimate_tokens({"state": state, "questions": questions}, ratio=ratio)
