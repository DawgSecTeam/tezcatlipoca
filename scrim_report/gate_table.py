RESTORE_TTR_GATE_MIN = 15


GATES = {
    "red": [("takedowns", 6, ">="), ("restore_reactions", 3, ">="),
            ("distinct_tactics", 4, ">="), ("windows_footholds", 1, ">="),
            ("max_simultaneous_down", 4, ">="), ("stalls", 0, "=="),
            ("evictions", 1, ">=")],
    # cycles_rc0 tuned 8 -> 6 (2026-10-12) per docs/rehearsal-gates.md's
    # post-rehearsal protocol: two full rehearsals on the GX10 qwen-flash
    # measure 8-17 min per meaningful blue cycle even scoped to ONE change
    # (run3: 2/2 overruns of a 1800 s cap; run4: 1 rc=0 + 5 overruns of 540 s;
    # run5: 4 rc=0 + 2 timeouts of 600 s), so >= 8 in a 60-min event was
    # unreachable at zero timeouts. Must stay in sync with the doc's row.
    "blue": [("cycles_rc0", 6, ">="), ("restorations", 2, ">="),
             ("injects", 2, ">="), ("notebook_entries", 10, ">="),
             ("timeouts", 0, "==")],
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
                rows.append((section, key, "n/a", threshold, "n/a"))
                continue
            ok = val >= threshold if op == ">=" else val == threshold
            rows.append((section, key, val, threshold, "PASS" if ok else "FAIL"))
    return rows


def op_str(threshold):
    return f"== {threshold}" if threshold == 0 else f">= {threshold}"
