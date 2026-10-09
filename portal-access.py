#!/usr/bin/env python3
"""Open, close, or inspect a competition's student-portal gate without the web UI.

    python3 portal-access.py <competition> status
    python3 portal-access.py <competition> open
    python3 portal-access.py <competition> close
    python3 portal-access.py <competition> resync    # re-apply console ACLs after a box rebuild

Team consoles stay closed (HTTP 423) until access is opened; admins always bypass it. The
white-team view in the portal flips the same switch (docs/portal.md).

`resync` re-applies this run's console-token ACLs (one per team VM). Proxmox's VM-destroy path
removes that VM's ACL entries (not yet observed on this estate — confirm on the first rebuild
drill), so a box rebuilt by redeploy-competition.py can lose its console until this (or a
re-run of phase 8) puts the ACL back. The PUTs are idempotent, so running it needlessly is
harmless."""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(".env"))

from config_ops import write_state  # noqa: E402
from nodes_ops import activate_placement, read_placement  # noqa: E402
from portal_ops import access_state, portal_enabled, set_access  # noqa: E402
from pve_console_ops import mint_console_tokens, node_targets  # noqa: E402
from ssh_ops import read_terraform_ctx  # noqa: E402
from targets import load_targets  # noqa: E402


def resync(comp_dir):
    """Re-apply this run's console ACLs on every node (the token itself is reused)."""
    import os

    state_path = comp_dir / ".deploy_state.json"
    state = json.loads(state_path.read_text())
    teams = json.loads((comp_dir / "teams.json").read_text())
    boxes = json.loads((comp_dir / "boxes.json").read_text())
    placement = read_placement(comp_dir)
    if placement:
        activate_placement(placement)
    nodes = node_targets(load_targets(comp_dir, teams, boxes), placement,
                         os.environ.get("TF_VAR_proxmox_node", "pve"))
    previous = state.get("portal_console_tokens") or {}
    tokens, problems = mint_console_tokens(comp_dir.name, state["run_id"], nodes,
                                           previous=previous)
    for p in problems:
        print(f"  WARNING: {p}")
    if any(tokens.get(k, {}).get("secret") != v.get("secret") for k, v in previous.items()):
        # The token had to be re-created (PVE only shows a secret at creation), so the
        # engine's portal.json is stale: re-run phase 8 to re-ship it.
        print("  NOTE: a console token was re-created — re-run "
              f"`create-competition.py --competition {comp_dir.name} --from-phase 8 --yes` "
              "so the portal picks up the new secret.")
    state["portal_console_tokens"] = {**previous, **tokens}
    write_state(state_path, state)
    print(f"  console ACLs re-applied on {len(tokens)} node(s)")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("competition")
    parser.add_argument("action", choices=["status", "open", "close", "resync"])
    args = parser.parse_args()
    comp_dir = Path("competitions") / args.competition
    if not comp_dir.is_dir():
        sys.exit(f"no such competition: {comp_dir}")
    if not portal_enabled(comp_dir):
        sys.exit(f"{args.competition} does not run the student portal (Compfile `portal 1`)")
    if args.action == "resync":
        resync(comp_dir)
        return
    tf_ctx = read_terraform_ctx(comp_dir)
    if args.action != "status":
        set_access(tf_ctx, args.action == "open")
    state = access_state(tf_ctx)
    if state is None:
        sys.exit("portal did not answer /healthz on the engine")
    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
