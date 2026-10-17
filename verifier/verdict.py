"""Run-level verdict: exit-code derivation and SUMMARY lines from GateResults."""

from verifier.model import Status


_GATE_LABEL_WIDTH = 18


def gate_verdict(results, allow_unverified=()):
# (gate, passed) derived from GateResults: a SKIP is non-passing (never exit 0) unless the
#     operator named that gate in --allow-unverified; a FAIL always fails.
    allowed = set(allow_unverified)

    gate = {r.name: r.passed for r in results if r.gating}
    passed = all(r.status is Status.PASS
                 or (r.status is Status.SKIP_UNAVAILABLE
                     and (r.name in allowed or r.label in allowed))
                 for r in results if r.gating)
    return gate, passed


def summary_lines(results, allow_unverified=()):
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
