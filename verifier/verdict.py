"""Run-level verdict: --timeout budget, exit-code derivation and SUMMARY lines from GateResults."""

import time

from verifier.model import Status


class RunBudget:
    """Optional whole-run wall-clock budget for --timeout (default: disabled).

    Additive, not a re-gate: with --timeout absent every query says `expired()` is
    False, so the run is byte-for-byte what it was before. Deliberately cooperative —
    it is checked BETWEEN gates, never mid-flight, so it can never kill an ssh or
    interrupt a Proxmox task half-done (a half-deleted snapshot or half-converted
    golden is worse than a slow verify); worst-case overshoot is the one gate already
    running. A gate that never ran is recorded SKIP_UNAVAILABLE — deliberately
    non-passing — so a budget can bound a verify but can never turn an unevaluated
    range into a PASS; --allow-unverified is the explicit waiver.
    """

    def __init__(self, seconds):
        self.seconds = int(seconds or 0)
        self.deadline = time.monotonic() + self.seconds if self.seconds > 0 else None
        self.label = f"--timeout {self.seconds}s"

    def expired(self):
        return self.deadline is not None and time.monotonic() >= self.deadline


_GATE_LABEL_WIDTH = 18


def gate_verdict(results, allow_unverified=()):
    """(gate, passed) derived from GateResults.

    gate maps every gating gate name to `status is PASS` (what do_freeze records).
    A SKIP is non-passing — it must not yield exit 0 — unless the operator named
    that gate in --allow-unverified; a FAIL always fails."""
    allowed = set(allow_unverified)

    def _allowed(r):
        return r.name in allowed or r.label in allowed

    gate = {r.name: r.passed for r in results if r.gating}
    passed = all(r.status is Status.PASS
                 or (r.status is Status.SKIP_UNAVAILABLE and _allowed(r))
                 for r in results if r.gating)
    return gate, passed


def summary_lines(results, allow_unverified=()):
    """The SUMMARY body, generated from the GateResults themselves (D6)."""
    allowed = set(allow_unverified)
    lines = []
    for r in results:
        line = f"  {r.label:<{_GATE_LABEL_WIDTH}}: {r.status.value}"
        if r.detail:
            line += f"  {r.detail}"
        if not r.gating:
            line += " [informational]"
        elif r.status is Status.SKIP_UNAVAILABLE and (r.name in allowed or r.label in allowed):
            line += " (allowed — --allow-unverified)"
        lines.append(line)
    return lines
