"""The compact typed state digest: budgeted against the model's batch; the truncation cascade may drop detail but never ROE/phase/caps."""

import json
import math


# Measured ~0.55-0.93 tokens/char for these bundles (chars/4 is wildly
# optimistic for compact JSON); raised from the endpoint's own "too large" reports.
TOKENS_PER_CHAR = 0.7


def estimate_tokens(obj, ratio=None):
    text = obj if isinstance(obj, str) else dumps(obj)
    return max(1, math.ceil(len(text) * float(ratio or TOKENS_PER_CHAR)))


def dumps(obj):
    """Compact separators, keys in insertion order; credential material never
    reaches this layer."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True,
                      default=str)


def _untouched(box):
    return (not box.get("acc") and not box.get("ch")
            and all(s[1] == "up" for s in box.get("svc", []) if len(s) > 1))


def build(frame, budget_tokens=8192):
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

    while digest["recent"] and estimate_tokens(digest) > budget_tokens:
        digest["recent"].pop(0)
        if "recent" not in dropped:
            dropped.append("recent_tails")
    if estimate_tokens(digest) > budget_tokens:
        for box in digest["boxes"]:
            box.pop("resp", None)
        dropped.append("box_resp")
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
    # This may legitimately empty the list: a digest with no interesting box is
    # the truth, not a padded one.
    if estimate_tokens(digest) > budget_tokens:
        kept = [b for b in digest["boxes"] if not _untouched(b)]
        if len(kept) != len(digest["boxes"]):
            digest["boxes"] = kept
            dropped.append("untouched_boxes")
    if estimate_tokens(digest) > budget_tokens:
        for box in digest["boxes"]:
            if isinstance(box.get("svc"), list) and len(box["svc"]) > 2:
                box["svc"] = box["svc"][:2]
            box.pop("os", None)
            box.pop("team", None)
        dropped.append("box_trim")

    # Floor: two boxes as identity+access only; without it a wide board never
    # fits a small batch and the whole event degrades to deterministic picks.
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
