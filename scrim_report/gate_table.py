RESTORE_TTR_GATE_MIN = 15
# A large comp still demands this much sustained pressure; a small lineup cannot
# reach it without re-kills (which need blue to restore first), so render.py caps
# the per-run threshold at what red alone can take down. See docs/rehearsal-gates.md.
TAKEDOWNS_GATE = 6


GATES = {
    "red": [("takedowns", TAKEDOWNS_GATE, ">="), ("restore_reactions", 3, ">="),
            ("distinct_tactics", 4, ">="), ("windows_footholds", 1, ">="),
            ("max_simultaneous_down", 4, ">="), ("stalls", 0, "==")],
    # evictions is deliberately NOT a gate: red's health checks only see SSH-class
    # footholds, so zero is an observability gap, not a failed red.
    # blue's cadence gates assume one shared LLM lock: a real cycle costs 20-30 min and the two
    # teams serialise, so a short scrim affords about one cycle per team. Meaning: rehearsal-gates.md.
    "blue": [("cycles_rc0", 2, ">="), ("restorations", 2, ">="),
             ("injects", 2, ">="), ("notebook_entries", 10, ">="),
             ("timeouts", 2, "<=")],
}


# thresholds overrides a gate's static threshold by key where the right threshold is
# per-run data (max_simultaneous_down judges against red's own max_concurrent_down_end
# pacing cap, not a hardcoded 4); gates replaces the static GATES table per section
# (used to drop a structurally inapplicable gate)
def evaluate(gm, bm, thresholds=None, gates=None):
    thresholds = thresholds or {}
    gates = gates or GATES
    rows = []
    for section, metrics in (("red", gm), ("blue", bm)):
        for key, threshold, op in gates[section]:
            threshold = thresholds.get(key, threshold)
            val = metrics.get(key)
            if val is None:
                rows.append((section, key, "n/a", threshold, "n/a", op))
                continue
            ok = (val <= threshold if op == "<="
                  else val >= threshold if op == ">=" else val == threshold)
            rows.append((section, key, val, threshold, "PASS" if ok else "FAIL", op))
    return rows


def op_str(threshold, op="=="):
    return f"<= {threshold}" if op == "<=" else (
        f"== {threshold}" if op == "==" else f">= {threshold}")
