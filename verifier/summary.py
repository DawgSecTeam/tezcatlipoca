"""The SUMMARY block printed after every verify run (gate lines + deploy/freeze facts)."""

import json

from verifier.freeze import freeze_hashes
from verifier.verdict import summary_lines


def print_summary(results, allow_unverified, comp_dir):
# The SUMMARY block: per-gate lines (from the GateResults), the recorded template hashes,
#     and the freeze marker.
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
    hashes = freeze_hashes(comp_dir)
    if hashes and (hashes.get("engine") or {}).get("hash"):
        golden_short = {k: v["hash"][:12] for k, v in (hashes.get("golden") or {}).items()}
        print(f"  templates        : engine {hashes['engine']['hash'][:12]} | "
              f"golden {json.dumps(golden_short)}")
    from engine.template_freeze import frozen_state
    frozen = frozen_state(comp_dir)
    if frozen:
        print(f"  freeze           : FROZEN since {frozen.get('frozen_at') or '?'}")
