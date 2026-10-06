from pathlib import Path

from scrim_report import blue_side
from scrim_report import host_labels
from scrim_report import loaders
from scrim_report import red_side


RESTORE_TTR_GATE_MIN = 15


GATES = {
    "red": [("takedowns", 6, ">="), ("restore_reactions", 3, ">="),
            ("distinct_tactics", 4, ">="), ("windows_footholds", 1, ">="),
            ("max_simultaneous_down", 4, ">="), ("stalls", 0, "=="),
            ("evictions", 1, ">=")],
    "blue": [("cycles_rc0", 8, ">="), ("restorations", 2, ">="),
             ("injects", 2, ">="), ("notebook_entries", 10, ">="),
             ("timeouts", 0, "==")],
}


EXPECTED_17C = {
    "takedowns": 4, "distinct_tactics": 3, "restore_reactions": 1,
    "restorations": 1, "injects": 0, "interaction_score": 2, "stalls": 2,
    "evictions": 0, "timeouts": 0,
}


def evaluate(gm, bm, thresholds=None, gates=None):
    """Gate rows. `thresholds` overrides a gate's static threshold by key — used where the
    right threshold is per-run data (max_simultaneous_down judges against red's own
    max_concurrent_down_end pacing cap, not a hardcoded 4). `gates` replaces the static
    GATES table per section (used to drop a structurally inapplicable gate)."""
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


def compute_components(run_dir):
    """Score components alone, for the pinned self-test."""
    events, t0 = loaders.load_red_events(run_dir)
    world = loaders.load_world(run_dir)
    snaps = loaders.load_scoreboard(run_dir)
    rm = red_side.red_metrics(events, t0, world, host_labels.box_labels(run_dir), host_labels.windows_octets(run_dir))
    down = blue_side.down_windows(snaps)
    bm = blue_side.blue_metrics(run_dir)
    restorations = (sum(d["restorations"] for d in down.values()) if down
                    else rm["restore_reactions"])
    return {"takedowns": rm["takedowns"], "distinct_tactics": len(rm["distinct_tactics"]),
            "restore_reactions": rm["restore_reactions"], "restorations": restorations,
            "injects": bm["injects"], "stalls": len(rm["stalls"]),
            "evictions": rm["evictions"], "timeouts": bm["timeouts"],
            "interaction_score": restorations + rm["restore_reactions"]
            + rm["evictions"] + bm["injects"] + bm["eradication"]}


def self_test(run_dir):
    comp = compute_components(run_dir)
    bad = [f"  {k}: expected {v}, got {comp.get(k)}"
           for k, v in EXPECTED_17C.items() if comp.get(k) != v]
    if bad:
        print("SELF-TEST FAILED (pin mismatch — re-check against FINDINGS.md evidence):")
        print("\n".join(bad))
        return 1
    print(f"self-test OK: {Path(run_dir).name} scores exactly the pinned 17c numbers")
    return 0
