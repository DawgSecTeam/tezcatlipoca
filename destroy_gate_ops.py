"""Teardown gates: every refusal that must fire before anything on the node is touched.

Kept in one place so the ownership anchor (the deploy's run id), the endpoint guard and
the frozen-competition refusal cannot be reordered or skipped by a mode added later.
`destroy-competition.py` calls these before any Proxmox mutation (artifact collection,
pre-stop, terraform destroy, template teardown)."""

import json
import os
import sys


def refuse_frozen_full_teardown(competition, frozen, full, end_of_competition):
    """A frozen competition's templates are the verified artifacts the event runs on: a
    --full teardown needs --end-of-competition."""
    if full and frozen and not end_of_competition:
        print(f"  ERROR: '{competition}' is FROZEN (frozen_at {frozen.get('frozen_at')}). "
              f"A full teardown would destroy the verified templates mid-competition. "
              f"Re-run with --end-of-competition if the event is genuinely over.")
        sys.exit(1)


def load_ownership(comp_dir):
    """Read .deploy_state.json ONCE and enforce the destruction-ownership anchor.

    Returns (deployed_state, run_id). Exits when the state carries no run id (ownership
    cannot be proven) or when the loaded env targets a different Proxmox endpoint than
    the deploy used."""
    deployed_state = {}
    state_path = comp_dir / ".deploy_state.json"
    if state_path.exists():
        try:
            deployed_state = json.loads(state_path.read_text())
        except (ValueError, OSError):
            deployed_state = {}
    run_id = deployed_state.get("run_id") or ""
    if not run_id:
        raise SystemExit(
            f"  ERROR: {state_path} carries no run id, so ownership of this competition's VMs "
            f"cannot be proven and nothing will be destroyed. Every deploy since 0.2.0 stamps "
            f"one; a range without it predates 0.2.0 — remove it by hand on the node.")
    if deployed_state:
        deployed = (deployed_state.get("deployed_endpoint") or "").rstrip("/")
        current = os.environ.get("TF_VAR_proxmox_endpoint", "").rstrip("/")
        if deployed and current and deployed != current:
            raise SystemExit(
                f"  ERROR: this competition was deployed against {deployed}, but the "
                f"loaded env targets {current}. Point TF_VAR_* at the deployment's host "
                f"(same overrides create-competition ran with) and re-run.")
    return deployed_state, run_id
