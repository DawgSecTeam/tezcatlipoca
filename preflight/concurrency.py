"""Gate: warn loudly when another deploy is live on this host, then proceed."""

from pathlib import Path


def gate_concurrent_deploys():
    """Warn loudly when another deploy is live on this host, then proceed.

    Concurrency is the supported shape (2026-10-04): worktree-isolated runs with their
    own `--scoring-vmid` / `TF_VAR_team_identifiers` blocks coexist by design, and the
    gates that refuse REAL conflicts are per-resource — the vmid-collision and
    engine-mgmt-IP checks, plus run-id ownership tags scoping every teardown. This flock
    check stays as the loud signal, not a gate: the locks directory is the one
    concurrency signal that cannot lie (a holder is a live process, the kernel releases
    the flock when it dies, so a leftover `.lock` file is never a false positive), which
    is exactly what a WARNING should be built on. Two UNCOORDINATED sessions sharing vmid
    blocks is still the documented cause of the 13xx vmid races, the foreign-golden squat
    and the over-broad sweep (AGENTS.md) — the warning names the holder so the operator
    can check before the collision gates have to.

    The signal is only meaningful because of the call order in `deploy_lib.prepare`: the
    engine lock is taken BEFORE the preflight runs this gate, so any deploy that has got
    far enough to matter is already holding a lock."""
    from nakon_ops import other_deploys_in_flight

    in_flight = other_deploys_in_flight()
    if not in_flight:
        return
    summary = ", ".join(f"{Path(path).name} ({int(age)}s)" for path, age in in_flight[:4])
    if len(in_flight) > 4:
        summary += f", +{len(in_flight) - 4} more"
    print(f"  WARNING: {len(in_flight)} other deploy(s) in flight on this host ({summary}). "
          f"Concurrent ranges are supported — give this one its own --scoring-vmid and "
          f"TF_VAR_team_identifiers blocks (the preflight refuses real collisions); "
          f"teardowns are scoped by run-id ownership tags.")
