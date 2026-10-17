"""The compact typed state digest — typed, ordered, and budgeted.

clef accepts any JSON `state`, and the whole request (digest + question bundle)
must fit the endpoint's batch. That budget is now generous (16k, up from the
512-token `--ubatch-size` the v2 design was originally
written against), so in practice the full state goes out and nothing is cut.
The digest keeps its fixed shape and its truncation cascade anyway: it is the
guard that lets the same code run against a small-batch endpoint or a different
model without a code change, and it is what makes the request size predictable
instead of ad hoc.

Truncation order (plan §2, extended by what the small-batch endpoint actually
rejected — only reachable now when `budget_tokens` is set small):
drop `recent` tails -> drop per-box `resp` detail -> cap boxes at 2 per team ->
drop untouched boxes -> per-box trim (2 services, no redundant keys) -> minimal
digest (two boxes as identity+access, no `recent`/`more`/service rows, `roe`
reduced to win+modes). NEVER drop ROE / phase / caps. If the digest still cannot
fit it is flagged (`_over_budget`) and the decision layer degrades
deterministically instead of silently sending a truncated question.

The token estimate is CALIBRATED (`TOKENS_PER_CHAR`): these bundles cost ~0.55-0.93
tokens/char, not the naive 0.25, and the whole request (digest + question bundle)
must fit the model's physical batch. `Decider.compose()` builds the bundle first
and gives the digest the remainder; the budget passed here is in estimated
tokens, not characters.
"""

import json
import math


# How many tokens a character costs. MEASURED, not assumed: chars/4 was wildly
# optimistic for compact JSON, and the old 512-token endpoint answered a bundle
# our estimator called small with "input (960 tokens) is too large to process" —
# which turned every cycle deterministic while every test stayed green. Measured:
# ~0.55 tokens/char for a compact bundle, ~0.93 for a wide one (JSON punctuation
# and short ids tokenize badly). 0.7 is the working default; the client raises it
# from the endpoint's own "input (N tokens) is too large" report, which is the
# only authority on its tokenizer. Still worth keeping honest now that the limit
# is 16k: the same number sizes requests against any endpoint.
TOKENS_PER_CHAR = 0.7


def estimate_tokens(obj, ratio=None):
    text = obj if isinstance(obj, str) else dumps(obj)
    return max(1, math.ceil(len(text) * float(ratio or TOKENS_PER_CHAR)))


def dumps(obj):
    """Canonical serialization: compact separators, keys kept in insertion
    order (callers build ordered dicts; nested credential material never
    reaches this layer)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True,
                      default=str)


def _untouched(box):
    return (not box.get("acc") and not box.get("ch")
            and all(s[1] == "up" for s in box.get("svc", []) if len(s) > 1))


def build(frame, budget_tokens=8192):
    """frame -> (digest, dropped_fields). frame keys (all optional except t):
      t        "95/150" elapsed/total minutes
      phase    e.g. surge|observe|endgame|quiet
      cap      int pressure cap this cycle
      down     int services down now
      boxes    [{id, team, os, acc, svc:[[name,state],...], resp:{}, ch:{}}]
      recent   ["stop_mask web01 nginx ok", ...] most-recent-last, short strings
      roe      {"win": "...", "modes": [...], "tier": "..."}
    """
    digest: dict = {"v": 2}
    for key in ("t", "phase", "cap", "down"):
        if key in frame:
            digest[key] = frame[key]
    digest["boxes"] = [dict(b) for b in frame.get("boxes", [])]
    digest["recent"] = list(frame.get("recent", []))[-24:]
    digest["roe"] = dict(frame.get("roe", {}))

    dropped = []
    if estimate_tokens(digest) <= budget_tokens:
        return digest, dropped

    # 1. recent tails
    while digest["recent"] and estimate_tokens(digest) > budget_tokens:
        digest["recent"].pop(0)
        if "recent" not in dropped:
            dropped.append("recent_tails")
    # 2. per-box resp detail
    if estimate_tokens(digest) > budget_tokens:
        for box in digest["boxes"]:
            box.pop("resp", None)
        dropped.append("box_resp")
    # 3. cap boxes at 2 per team (keep the ones with an acc or a channel first)
    if estimate_tokens(digest) > budget_tokens:
        per_team = {}
        kept = []
        for box in sorted(digest["boxes"],
                          key=lambda b: (not b.get("acc"), not b.get("ch"), b["id"])):
            team = box.get("team")
            if per_team.get(team, 0) < 2:
                per_team[team] = per_team.get(team, 0) + 1
                kept.append(box)
        if len(kept) != len(digest["boxes"]):
            dropped.append("boxes_per_team_cap")
        digest["boxes"] = kept
    # 4. untouched boxes (no access, no channel, nothing down). This step may
    #    legitimately empty the list: a digest with no interesting box is the
    #    truth, and an empty list is smaller than a padded one.
    if estimate_tokens(digest) > budget_tokens:
        kept = [b for b in digest["boxes"] if not _untouched(b)]
        if len(kept) != len(digest["boxes"]):
            digest["boxes"] = kept
            dropped.append("untouched_boxes")
    # 5. per-box trim: two service entries, no redundant keys
    if estimate_tokens(digest) > budget_tokens:
        for box in digest["boxes"]:
            if isinstance(box.get("svc"), list) and len(box["svc"]) > 2:
                box["svc"] = box["svc"][:2]
            box.pop("os", None)
            box.pop("team", None)
        dropped.append("box_trim")

    # 6. last resort: one box per team and no service rows at all. A box's
    #    identity and access state are what a decision needs; its service list is
    #    a convenience. Without this floor a wide board cannot fit a small batch
    #    at all, and the run would fall back to deterministic picks for the whole
    #    event.
    if estimate_tokens(digest) > budget_tokens:
        kept = []
        for box in digest["boxes"][:2]:
            kept.append({k: v for k, v in box.items()
                         if k in ("id", "acc", "ch", "dead")})
        digest["boxes"] = kept
        digest.pop("recent", None)
        digest.pop("more", None)
        roe = digest.get("roe") or {}
        digest["roe"] = {k: v for k, v in roe.items() if k in ("win", "modes")}
        dropped.append("digest_minimal")

    if estimate_tokens(digest) > budget_tokens:
        digest["_over_budget"] = True
        dropped.append("budget_exceeded")
    return digest, dropped
