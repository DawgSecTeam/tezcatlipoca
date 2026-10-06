"""The SUMMARY block printed after every verify run (gate lines + deploy/freeze facts)."""

import json

from verifier.freeze import freeze_hashes
from verifier.loaders import read_deploy_state
from verifier.verdict import summary_lines


def print_summary(results, allow_unverified, comp_dir):
    """The SUMMARY block: per-gate lines (from the GateResults), the plant-integrity tally,
    the recorded template hashes, and the freeze marker."""
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for line in summary_lines(results, allow_unverified):
        print(line)
    unknown_allowed = sorted(set(allow_unverified)
                             - {r.name for r in results} - {r.label for r in results})
    if unknown_allowed:
        print(f"  WARN  --allow-unverified names no gate in this run: "
              f"{', '.join(unknown_allowed)}")
    state = read_deploy_state(comp_dir)
    tally = state.get("nakon_failed_steps") if state is not None else None
    if tally is None:
        print("  plant integrity  : no nakon FAILED tally in .deploy_state.json "
              "(pre-tally deploy)")
    elif tally:
        print(f"  plant integrity  : WARNING — last nakon plant recorded {len(tally)} "
              f"FAILED step(s): {', '.join(str(s)[:60] for s in tally[:3])}"
              f"{' …' if len(tally) > 3 else ''}")
    else:
        print("  plant integrity  : last nakon plant recorded 0 FAILED steps")
    hashes = freeze_hashes(comp_dir)
    if hashes and (hashes.get("engine") or {}).get("hash"):
        golden_short = {k: v["hash"][:12] for k, v in (hashes.get("golden") or {}).items()}
        print(f"  templates        : engine {hashes['engine']['hash'][:12]} | "
              f"golden {json.dumps(golden_short)}")
    frozen_path = comp_dir / ".frozen.json"
    if frozen_path.exists():
        try:
            frozen_at = json.loads(frozen_path.read_text()).get("frozen_at")
        except (OSError, ValueError):
            frozen_at = "?"
        print(f"  freeze           : FROZEN since {frozen_at}")
