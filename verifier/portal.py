"""Student portal gate: the portal answers, logins work, scoping holds, consoles relay.

Runs portal_ops.check_portal's probe ON the engine against the portal's 127.0.0.1 listener (the
same path cloudflared uses): health, a team login that sees exactly its own boxes, the gate
(423 while closed), a cross-team console request refused (403), an admin login, and per
Proxmox node one console minted and relayed far enough to read the RFB greeting, then the same
relay id refused on reuse. Admin consoles bypass the gate, so the console rows prove the relay
whether or not access is open yet."""

import json
import subprocess

from portal_ops import check_portal, portal_enabled

from verifier import context
from verifier.model import CheckError, gate_fail, gate_pass, gate_skip


def check_portal_gate(ctx, comp_dir, teams, admin_password):
    """GateResult: SKIP when the comp runs no portal; FAIL on any failed probe row; SKIP when
    the engine can't be reached (an unrun check is not a pass)."""
    if not portal_enabled(comp_dir):
        return gate_skip("portal", "competition runs no student portal (Compfile `portal`)",
                         gating=False)
    print("\n  (student portal — login, team scoping, gate, console relay)")
    try:
        config = json.loads((comp_dir / "portal.json").read_text())
    except (OSError, ValueError):
        print("  FAIL  portal.json missing — the deploy never built the portal")
        return gate_fail("portal", "portal.json missing (did phase 8 record a portal degradation?)")

    def run_engine(cmd):
        try:
            proc = context.ssh_to_engine(ctx, cmd, timeout=120)
        except subprocess.TimeoutExpired as e:
            raise CheckError(f"engine SSH timed out ({e})")
        return proc.returncode, proc.stdout or ""

    try:
        rows = check_portal(run_engine, comp_dir.name, teams, admin_password, config)
    except CheckError as e:
        print(f"  SKIP  — could not reach the engine to probe the portal ({e})")
        return gate_skip("portal", "engine unreachable")
    except RuntimeError as e:
        print(f"  FAIL  {e}")
        return gate_fail("portal", str(e)[:200])
    failures = []
    for row in rows:
        word = "PASS" if row["ok"] else "FAIL"
        print(f"  {word}  {row['check']}: {row['detail']}")
        if not row["ok"]:
            failures.append(f"{row['check']}: {row['detail']}")
    if not config.get("nodes"):
        print("  NOTE  no console token on any node — consoles are off (logins/scoping checked)")
    if failures:
        return gate_fail("portal", "; ".join(failures)[:200])
    return gate_pass("portal")
