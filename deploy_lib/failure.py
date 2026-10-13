"""Failure bookkeeping: the tolerated-failure ledger."""

from common.utils import degradation_summary, degradations


def record_degradations(ctx):
# Persist tolerated-prerequisite failures into the state and print them once.
#
#     Every entry is something that failed while the deploy continued: apt prep that
#     installed nothing, an auth ladder that failed on both transports, a box that never
#     settled, a node scan that never happened. They used to exist only as WARNING lines
#     in the scrollback — which is why "the range came up with every service down" had no
#     named cause. Now they land in .deploy_state.json and verify can read them.
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
