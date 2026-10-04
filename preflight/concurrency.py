"""Gate: refuse to start while another deploy is live on this host."""

import os
from pathlib import Path


def gate_concurrent_deploys():
    """Refuse to start while another deploy is live on this host.

    The locks directory is the one concurrency signal that cannot lie: a holder is a
    live process, and the kernel releases the flock when it dies, so a leftover `.lock`
    file is never a false positive (unlike a mtime, a running VM, or a log). Two sessions
    sharing one estate is the documented cause of the 13xx vmid races, the foreign-golden
    squat and the over-broad sweep that took out two competitions' engines and goldens
    (AGENTS.md, docs/environment-facts.md).

    The signal is only meaningful because of the call order in `deploy.prepare()`:
    `_apply_engine_placement` takes the engine lock BEFORE `_run_competition_preflight`
    runs this gate. Any deploy that has got far enough to matter is therefore holding a
    lock. The only uncovered window is the few seconds a second process spends loading
    config before it takes its own lock.

    TEZ_ALLOW_CONCURRENT=1 proceeds anyway — for a deliberately coordinated second range
    with its own vmid blocks, which is the only safe way to run two at once.
    """
    from nakon_ops import other_deploys_in_flight

    in_flight = other_deploys_in_flight()
    if not in_flight:
        return
    summary = ", ".join(f"{Path(path).name} ({int(age)}s)" for path, age in in_flight[:4])
    if len(in_flight) > 4:
        summary += f", +{len(in_flight) - 4} more"
    if os.environ.get("TEZ_ALLOW_CONCURRENT"):
        print(f"  WARNING: {len(in_flight)} other deploy(s) in flight on this host "
              f"({summary}) — proceeding because TEZ_ALLOW_CONCURRENT is set. Give this "
              f"range its own vmid blocks.")
        return
    raise SystemExit(
        f"  ERROR: {len(in_flight)} other deploy(s) are already running against this host "
        f"({summary}). Two concurrent deploys race for the same vmid blocks and golden "
        f"slots — the recorded outcome is a foreign template squatting this competition's "
        f"golden slot on the next run, and one sweep destroying another range's engines "
        f"(AGENTS.md). Wait for them (pgrep -af 'create-competition|redeploy-competition'), "
        f"or set TEZ_ALLOW_CONCURRENT=1 once you have coordinated distinct "
        f"--scoring-vmid / TF_VAR_team_identifiers blocks."
    )
