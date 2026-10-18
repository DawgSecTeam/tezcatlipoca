"""The decision layer: clef answers typed questions; everything else is a
bounded fallback.

The chain (plan §2), in order:

    candidates (policy.py, deterministic)
      -> typed question bundle -> clef (/v1/systemone) -> probabilities
      -> confidence gate (top_prob >= theta)
           act                        (high confidence)
           LLM arbiter                (low confidence AND high stakes)
           deterministic default      (low confidence AND low stakes)
      -> hard gate (ROE allowlist, kill switch, per-OS lint, preconditions)
      -> execute -> log EVERYTHING (bundle, answer, probabilities, threshold, source)

Hard rules that this module exists to enforce:

* **clef never opens a gate.** `command_intent` informs attribution and ranking;
  safety stays deterministic (exec/gate.py).
* **the run never depends on a model being up.** clef down -> deterministic
  defaults; LLM down -> clef still does everything but command authoring; both
  down -> the deterministic ladder. Every degradation is logged with its reason.
* **the probabilities are used, not the rounded answer.** `score` answers carry
  the expected value AND the per-level probabilities; the gate reads the top
  probability.
* **every decision is replayable offline.** A logged bundle re-runs through this
  module with a stubbed clef (tests/test_decide.py) — no model, no
  network, no range.
"""

import json
import re
import time
from dataclasses import dataclass, field

from .errors import ClefError, LLMError, PolicyError
from .state import digest as digest_mod

# The question catalog. Small on purpose — every question has to earn its place
# by replacing a heuristic that was wrong in the earlier agent (see the plan's
# table).
CATALOG = {
    "next_effect": {"type": "choice", "replaces": "3 override gates + fallback ladder"},
    "pressure_count": {"type": "score", "replaces": "press_impacts_per_cycle heuristics"},
    "escalate_now": {"type": "noul", "replaces": "restore-streak escalation rules"},
    "command_intent": {"type": "choice", "replaces": "_IMPACTY_WORDS substring sniff"},
    "step_verified": {"type": "noul", "replaces": "ok = any(rc == 0)"},
    "blue_hunting": {"type": "noul", "replaces": "ad-hoc tempo/restore heuristics"},
}

DEFAULT_THETA = 0.60
# The most a state digest may cost, however big the endpoint's batch is.
DIGEST_BUDGET_CEILING = 8192
INTENTS = ("takedown", "recon", "persistence", "benign")


# --------------------------------------------------------------------------- #
# the model client
# --------------------------------------------------------------------------- #

class ClefClient:
    """POST /v1/systemone — typed questions in, probabilities out, no generation.

    `poster` is injectable so the whole decision layer is testable offline; the
    default poster uses requests (imported lazily: the repo must be usable on a
    host without it, e.g. a bare red01 staging box).
    """

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
        # The endpoint's own token count beats our estimate: when it rejects a
        # request as too large it tells us the real number, so remember the ratio
        # it implies and never send that size again.
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

    # -- health -------------------------------------------------------------

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

    # -- the call -----------------------------------------------------------

    def ask(self, state, questions):
        """Returns {"answers": {...}, "usage": {...}} or raises ClefError."""
        if not self.available():
            raise ClefError("clef unavailable (disabled, unconfigured, or cooling "
                            f"down until {self.disabled_until:.0f})")
        body = {"state": state, "questions": questions}
        ratio = max(self.tokens_per_char, self.learned_ratio or 0.0)
        estimate = tokens_needed(state, questions, ratio=ratio)
        if estimate > self.max_request_tokens:
            # A design input, not a nuisance: the model rejects an oversized
            # batch, so an over-budget bundle is a caller bug. Fail loudly here
            # rather than letting the endpoint 500.
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
                # Learn the endpoint's real tokenizer cost and never send that
                # size again; the caller may retry once with a smaller bundle.
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
    """The endpoint's reported token count from a 'too large' rejection."""
    match = re.search(r"input \((\d+) tokens\) is too large", text or "")
    if match:
        return int(match.group(1))
    return None


class StubClef:
    """A recorded responder: `answers_by_question` or a callable.

    Used by the replay tests and by `--dry-run` smoke runs that must exercise the
    whole chain without a model endpoint.
    """

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


# --------------------------------------------------------------------------- #
# reading an answer
# --------------------------------------------------------------------------- #

def top_probability(answer):
    """The gate's number, computed from the probabilities (never the labels).

    `noul` answers carry a single yes-probability and no `probabilities` map, so
    they are handled first — reading the map first silently made every yes/no
    answer look like a 0.0-confidence answer.
    """
    if answer.get("type") == "noul" or ("noul" in answer and not answer.get("probabilities")):
        yes = float(answer.get("noul", 0.0) or 0.0)
        return max(yes, 1.0 - yes)
    probs = _probabilities(answer)
    if not probs:
        return 0.0
    return max(probs.values())


def choice_of(answer):
    """The winning option id, for a `choice` answer."""
    if answer.get("choice"):
        return answer["choice"]
    probs = _probabilities(answer)
    if not probs:
        return None
    return max(probs, key=lambda k: probs[k])


def score_of(answer):
    """Expected value of a `score` answer (0..n-1), plus the level distribution."""
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


# --------------------------------------------------------------------------- #
# the decision
# --------------------------------------------------------------------------- #

@dataclass
class Decision:
    question: str
    answer: object
    probabilities: dict = field(default_factory=dict)
    top_prob: float = 0.0
    theta: float = DEFAULT_THETA
    source: str = "deterministic"      # clef | llm_arbiter | deterministic
    chosen: dict = None                # the candidate (next_effect) or None
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

    # -- thresholds ---------------------------------------------------------

    def theta(self, question):
        clef = self.cfg.get("clef") or {}
        overrides = clef.get("theta_by_question") or {}
        try:
            return float(overrides.get(question, clef.get("theta", DEFAULT_THETA)))
        except (TypeError, ValueError):
            return DEFAULT_THETA

    def high_stakes(self, candidates, chosen_id=None):
        """High stakes = the configured effect list OR an explicit candidate flag.

        One rule, applied in one place: the candidate list carries the flag
        (policy.candidates sets it) and the config carries the list, so a
        candidate built by hand still gets the right treatment.
        """
        effects = tuple((self.cfg.get("clef") or {}).get("high_stakes_effects")
                        or ("takedown", "lockout", "rotate_credlist"))
        def risky(candidate):
            return bool(candidate.get("high_stakes")
                        or candidate.get("effect") in effects)
        if chosen_id is None:
            return any(risky(c) for c in candidates)
        hit = next((c for c in candidates if c.get("id") == chosen_id), None)
        return bool(hit and risky(hit))

    # -- the chain ----------------------------------------------------------

    def ratio(self):
        """The token/char ratio to size requests with (learned beats configured)."""
        return max(float(getattr(self.clef, "tokens_per_char",
                                 digest_mod.TOKENS_PER_CHAR) or 0.0),
                   float(getattr(self.clef, "learned_ratio", None) or 0.0))

    def compose(self, ctx, candidates, recent=None, *, cycle=0,
                include_escalation=False, include_blue_check=False, extra_shrink=0):
        """Questions first, then a digest sized to what is left of the batch.

        The bundle is built first, its cost measured, and the digest gets the
        remainder of the batch (minus a small envelope margin) — so a growing
        question bundle can never silently push the request over the endpoint's
        ceiling mid-event. The ceiling is 16k, which means the
        first attempt (full bundle, full digest) is the normal path; the compact
        and shrink attempts below survive as the fallback for a smaller endpoint.
        """
        ceiling = int((self.cfg.get("clef") or {}).get("max_request_tokens", 16384))
        ratio = self.ratio()
        digest = questions = meta = None
        # (compact?, digest shrink) attempts, cheapest first: a full bundle is
        # better for the model, so it is only given up when the batch demands it.
        attempts = [(False, 0), (True, 0), (True, 60), (True, 120)]
        if extra_shrink:
            attempts = [(True, extra_shrink + shrink) for _, shrink in attempts]
        for compact, shrink in attempts:
            # Compact mode also shortens the option list (4-5 rather than 8):
            # against a small batch the option TEXT is what the budget is spent
            # on, and the top five candidates carry the decision anyway.
            # include_pressure=False: the loop asks `pressure_count` as its own
            # (tiny) call, so bundling it here would spend the batch twice.
            questions, meta = self.policy.question_bundle(
                None, candidates, ctx, cycle=cycle, compact=compact,
                max_options=(3 if extra_shrink else 4) if compact else 8,
                include_escalation=include_escalation,
                include_blue_check=include_blue_check, include_pressure=False)
            overhead = tokens_needed({}, questions, ratio=ratio) + 24
            # A digest is a decision input, not a data dump: cap what we will
            # send even when the endpoint would take far more. The floor stays
            # low on purpose — against a SMALL batch the digest has to be able to
            # give ground, or the compact attempt can never fit either.
            budget = int(min(max(80, ceiling - overhead - shrink),
                             DIGEST_BUDGET_CEILING))
            digest = self.policy.frame(ctx, candidates, recent=recent,
                                      budget_tokens=budget)
            if tokens_needed(digest, questions, ratio=ratio) <= ceiling:
                if compact or extra_shrink:
                    self.log.emit("clef_bundle_compact", cycle=cycle,
                                  tokens=tokens_needed(digest, questions, ratio=ratio),
                                  ceiling=ceiling, tokens_per_char=round(ratio, 3))
                return digest, questions, meta
        # Nothing fit: hand back the smallest bundle we built. The client refuses
        # an over-budget request (ClefError) and the chain degrades
        # deterministically, which is a logged, designed behaviour — silently
        # sending a truncated question is not.
        self.log.emit("clef_bundle_over_budget", cycle=cycle,
                      tokens=tokens_needed(digest, questions, ratio=ratio),
                      ceiling=ceiling, tokens_per_char=round(ratio, 3),
                      state_chars=len(digest_mod.dumps(digest)),
                      questions_chars=len(digest_mod.dumps(questions)))
        return digest, questions, meta

    def _ask(self, cycle, digest, questions, bundle, *, ctx=None, candidates=None,
             recent=None, include_escalation=False, include_blue_check=False):
        """Ask clef; on an over-budget rejection, recompose once and retry.

        The endpoint is the authority on its own tokenizer: when it rejects a
        request, the client records the implied ratio and we shrink the bundle
        rather than spending the rest of the event on deterministic fallbacks.
        One retry, never a loop.
        """
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
        """Pick one candidate from the bounded list, and log the whole chain.

        `escalate_now` and `blue_hunting` are deliberately NOT bundled here: each
        is asked as its own (tiny) call at the moment it matters, which keeps the
        decision request inside the model's batch. The flags stay for a caller
        that wants them in one request.
        """
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
        bundle = asked["bundle"]
        meta = asked.get("meta", meta)
        payload = asked["payload"]
        if payload is None:
            # Degradation ladder, rung 1: clef down -> the deterministic
            # candidate #1 still acts. A model outage must not idle the run.
            chosen = candidates[0]
            return self._log(cycle, Decision(
                "next_effect", chosen["id"], source="deterministic", bundle=bundle,
                chosen=chosen, theta=theta,
                note=f"clef unavailable ({asked['error']}) — deterministic pick "
                     f"{chosen['id']}",
                meta={**meta, "fallback": chosen["id"]}))

        answer = (payload.get("answers") or {}).get("next_effect") or {}
        probs = _probabilities(answer)
        prob = top_probability(answer)
        picked = choice_of(answer)

        if prob >= theta and picked == "hold":
            return self._log(cycle, Decision(
                "next_effect", "hold", probs, prob, theta, "clef", None, bundle,
                note="clef chose to hold, above theta", meta=meta))
        if prob >= theta and picked:
            chosen = next((c for c in candidates if c["id"] == picked), None)
            if chosen is not None:
                return self._log(cycle, Decision("next_effect", picked, probs, prob,
                                                 theta, "clef", chosen, bundle,
                                                 meta=meta))
            # clef named something that is not on the list (hallucinated id):
            # the candidate list is the authority, so fall through.
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
                    note=f"{note}; arbiter chose {chosen['id']}: {why}", meta=meta))
        # rungs 2 and 3 of the ladder: the deterministic candidate #1 always
        # acts, so neither a dead clef nor a dead LLM idles the run.
        chosen = candidates[0]
        return self._log(cycle, Decision(
            "next_effect", chosen["id"], probs, prob, theta, "deterministic",
            chosen, bundle, note=f"{note} — deterministic pick {chosen['id']}",
            meta=meta))

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

    def pressure_count(self, digest, *, default=1, cycle=None):
        """How many extra takedowns this cycle (a `score` answer)."""
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
        return self._log(cycle, Decision(
            "pressure_count", value, _probabilities(answer), prob, theta, source,
            note=f"expected {expected:.2f} from levels {levels}"))

    def noul(self, question, digest, instructions, *, default=False, cycle=None):
        """A yes/no judgment (escalate_now, blue_hunting)."""
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

    # -- LLM-authored commands ----------------------------------------------

    def classify_command(self, command, os_name="linux"):
        """command_intent: inform attribution (never a gate).

        Deterministic keyword classification is the fallback *and* the sanity
        check: a `benign` classification from the model never turns an impacty
        command into a benign one (the earlier agent's substring sniff both missed
        real impact words and mis-attributed innocent ones).
        """
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

    # -- logging ------------------------------------------------------------

    def _log(self, cycle, decision):
        """Log one decision's whole chain (principle 7) and return it."""
        if cycle is not None:
            record = decision.as_log()
            self.log.decision(cycle=cycle, question=record["question"],
                              bundle=decision.bundle,
                              answer=record["answer"],
                              probabilities=record["probabilities"],
                              theta=record["theta"], source=record["source"],
                              chosen=record["chosen"], ok=None,
                              note=record["note"])
        return decision

    def record(self, cycle=None, decisions=(), *, executed=None):
        """Log a batch of decisions made outside the chain, plus what ran."""
        for decision in decisions:
            self._log(cycle, decision)
        if executed is not None:
            self.log.emit("decision_result", cycle=cycle, executed=executed)


# --------------------------------------------------------------------------- #
# deterministic helpers (also the replay fixtures)
# --------------------------------------------------------------------------- #

# Word-boundary regexes, not substrings: the earlier agent's substring sniff
# matched "id" inside "hidden" and missed real impact words in flags, and its
# mis-attribution went straight into the ledger. The order is the conservative one: impact first.
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
    """The deterministic read of a free-form command: the fallback for
    command_intent and the conservative floor under the model's answer."""
    text = str(command or "")
    if IMPACT_RE.search(text):
        return "takedown"
    if PERSIST_RE.search(text):
        return "persistence"
    if RECON_RE.search(text):
        return "recon"
    return "benign"


def replay_answer(record):
    """Rebuild the clef answer a logged decision was made from (replay tests)."""
    return {"type": "choice" if isinstance(record.get("answer"), str) else "noul",
            "choice": record.get("answer"),
            "probabilities": record.get("probabilities") or {}}


def tokens_needed(state, questions, ratio=None):
    """Budget estimate for a whole request, using the shared calibration."""
    from .state.digest import estimate_tokens
    return estimate_tokens({"state": state, "questions": questions}, ratio=ratio)
