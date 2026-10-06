"""Failure bookkeeping: the resume-loop streak and the tolerated-failure ledger."""

import re

from utils import degradation_summary, degradations


def record_degradations(ctx):
    """Persist tolerated-prerequisite failures into the state and print them once.

    Every entry is something that failed while the deploy continued: apt prep that
    installed nothing, an auth ladder that failed on both transports, a box that never
    settled, a node scan that never happened. They used to exist only as WARNING lines
    in the scrollback — which is why "the range came up with every service down" had no
    named cause. Now they land in .deploy_state.json and verify can read them.
    """
    entries = degradations()
    # ALWAYS record the key, even when empty. A gate cannot tell "clean" from "never
    # ran" if the success path writes nothing: verify reported the 2026-10-02 live
    # run's ledger as "not recorded by this deploy (pre-dates the ledger)", which reads
    # as a stale state file rather than the clean run it actually was. Same trap the
    # plant-coverage gate had to learn — and which verify caught on that same run.
    ctx.state["degradations"] = entries
    if not entries:
        return
    print(f"\n  [!] {len(entries)} tolerated failure(s) this run — the deploy continued "
          f"past each of them:")
    for item in degradation_summary():
        count = f" x{item['count']}" if item["count"] > 1 else ""
        print(f"      - {item['what']}{count}: {item['detail'][:120]}")
    print("      (recorded in .deploy_state.json as `degradations`; verify-competition "
          "surfaces them)")


def failure_signature(exc):
    """A stable identity for "the same failure again", for the resume budget.

    Raw messages differ every run (vmids, uuids, temp paths, elapsed seconds), so a
    naive signature would never match and the guard could never fire. Normalise the
    volatile parts and keep the shape. Erring toward *matching* is deliberate: the
    guard only ever refuses a resume, and --force-from-phase overrides it.
    """
    text = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
    text = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                  r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", "<uuid>", text)
    text = re.sub(r"\b[0-9a-fA-F]{12,}\b", "<hex>", text)
    text = re.sub(r"\d+", "N", text)
    return re.sub(r"\s+", " ", text).strip()[:160]


def record_failure(state, phase, exc):
    """Count consecutive failures of the same phase with the same signature.

    Returns the new count. A different signature at the same phase is a *new*
    failure, not a repeat, so the budget resets — that is why the streak is keyed on
    the signature and not just the phase.
    """
    signature = failure_signature(exc)
    previous = state.get("failure_streak") or {}
    repeated = (previous.get("phase") == phase
                and previous.get("signature") == signature)
    count = int(previous.get("count") or 0) + 1 if repeated else 1
    state["failure_streak"] = {"phase": phase, "signature": signature, "count": count}
    return count


def clear_failure_streak(state):
    """Drop the streak after a phase run that made it through."""
    return state.pop("failure_streak", None)
