"""Advisory gate: will the student portal (Compfile `portal 1`) be able to do its job?

Never refuses — the portal is non-fatal by design (portal_ops) — but its two failure modes are
far cheaper to hear about before an hour of cloning than at phase 8:

  - the deploy token cannot mint the per-comp console user/ACLs (pve_console_ops), which would
    leave the portal up with consoles off;
  - a practice run from a linked worktree is about to claim the event's stable public hostname
    (TEZ_PORTAL_TUNNEL_TOKEN in a copied .env) — portal_ops refuses only when another LIVE range
    answers on it, so an idle event hostname would be taken silently.
"""

import os
from pathlib import Path

from pve_console_ops import probe_permissions


def _linked_worktree(path):
    """True inside a linked git worktree (its `.git` is a file, not a directory)."""
    for d in [Path(path).resolve(), *Path(path).resolve().parents]:
        git = d / ".git"
        if git.exists():
            return git.is_file()
    return False


def _node_tokens(placement):
    if placement:
        for name, rec in placement["nodes"].items():
            yield name, rec["endpoint"], os.environ.get(rec["token_env"], "")
    else:
        yield (os.environ.get("TF_VAR_proxmox_node", "pve"),
               os.environ.get("TF_VAR_proxmox_endpoint", ""),
               os.environ.get("TF_VAR_proxmox_api_token", ""))


def portal_gate(comp_dir, placement=None, probe=probe_permissions):
    from portal_ops import portal_enabled

    if not portal_enabled(comp_dir):
        return
    for name, endpoint, token in _node_tokens(placement):
        if not endpoint or not token:
            print(f"  WARNING: student portal — no endpoint/token for node {name}; its consoles "
                  "will be off")
            continue
        try:
            missing = probe(endpoint, token)
        except Exception as e:  # noqa: BLE001 - advisory
            print(f"  WARNING: student portal — could not probe {name}'s token permissions "
                  f"({type(e).__name__}: {e})")
            continue
        if missing:
            print(f"  WARNING: student portal — the deploy token on {name} lacks "
                  f"{', '.join(missing)}: the portal will come up with consoles OFF on that "
                  "node (docs/portal.md → Operating it)")
    if os.environ.get("TEZ_PORTAL_TUNNEL_TOKEN") and _linked_worktree(comp_dir):
        print("  WARNING: student portal — TEZ_PORTAL_TUNNEL_TOKEN is set in a linked worktree. "
              "A practice run will claim the stable public hostname "
              f"({os.environ.get('TEZ_PORTAL_HOSTNAME') or 'unset'}) unless a live range "
              "already answers on it; unset it for practice runs.")
