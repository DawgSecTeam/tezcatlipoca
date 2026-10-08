"""Remote-access gate: the engine's tailnet wiring is live, serving the right subnets,
and the headscale policy carries this competition's scoped grants.

Config-presence level on purpose: proving what a PARTICIPANT can and cannot reach
needs an enrolled client device, which the verifier does not have — that probe is a
documented manual step (docs/remote-access.md). What CAN be asserted from here is
asserted: engine enrolled to the right control plane, every team subnet SERVING on
the router node, SNAT lines in the engine's nat table, this comp's tag-scoped ACL
lines in the live policy, and no other node advertising our subnets."""

import json
import os
import subprocess
from pathlib import Path

import remote_access_ops
from verifier import context
from verifier.model import CheckError, gate_fail, gate_pass, gate_skip


def _read_state(comp_dir):
    path = Path(comp_dir) / ".deploy_state.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _identifiers(teams):
    return sorted({str(t["identifier"]) for t in (teams or {}).values()
                   if isinstance(t, dict) and t.get("identifier") is not None})


def check_remote_access(comp_dir, teams, ctx):
    """GateResult. Feature off (or pre-feature comp) skips WITHOUT gating — a range
    that never enrolled says nothing about the gate either way. A range that DID
    enroll gets real checks; unreachable headscale is SKIP (never a pass)."""
    print("\n  (headscale remote access)")
    state = _read_state(comp_dir)
    ra = state.get("remote_access") or {}
    if not ra.get("enabled"):
        print("  SKIP  — remote access not enabled for this deploy (Compfile "
              "remote_access 0, or the comp predates the feature)")
        return gate_skip("remote_access", "not enabled for this deploy", gating=False)

    identifiers = _identifiers(teams)
    # Engine side: enrolled to OUR control plane.
    server_url = os.environ.get("TEZ_HEADSCALE_URL", "").rstrip("/")
    if not server_url:
        print("  FAIL  — TEZ_HEADSCALE_URL not set; cannot confirm the engine's "
              "control plane (the deploy required it)")
        return gate_fail("remote_access", "TEZ_HEADSCALE_URL unset")
    try:
        proc = context.ssh_to_engine(ctx, "sudo tailscale status --json 2>/dev/null || true")
    except CheckError as e:
        print(f"  SKIP  — engine SSH failed ({str(e)[:80]})")
        return gate_skip("remote_access", "engine unreachable")
    enrolled = False
    try:
        data = json.loads(proc.stdout or "{}")
        control = (data.get("CurrentTailnet") or {}).get("ControlURL") or ""
        enrolled = data.get("BackendState") == "Running" and server_url in control
    except ValueError:
        enrolled = False
    if proc.returncode != 0 or not enrolled:
        print("  FAIL  — engine tailscale is not Running against "
              f"{server_url} (BackendState/control mismatch)")
        return gate_fail("remote_access", "engine not enrolled to the headscale control plane")
    print(f"  PASS  engine enrolled to {server_url}")

    # Engine side: the SNAT unit's rules are actually in the nat table.
    snat_probe = "; ".join(
        f"sudo iptables -t nat -S POSTROUTING | grep -q 'd 192.168.{i}.0/24 -j SNAT' && "
        f"echo ok{i} || echo MISSING{i}" for i in identifiers)
    proc = context.ssh_to_engine(ctx, snat_probe)
    missing = [ln for ln in (proc.stdout or "").split() if ln.startswith("MISSING")]
    if proc.returncode != 0 or missing:
        print(f"  FAIL  — tailnet->team SNAT lines absent for: "
              f"{', '.join(m.split('MISSING')[1] for m in missing) or 'unknown (rc)'}")
        return gate_fail("remote_access", "SNAT lines missing from the engine nat table")
    print(f"  PASS  SNAT present for {len(identifiers)} team subnet(s)")

    # Headscale side: routes serving, no foreign overlap, policy carries the grants.
    try:
        ok, problems = remote_access_ops.verify_remote_access(state, ra.get("comp_id"),
                                                              identifiers)
    except (subprocess.SubprocessError, OSError) as e:
        print(f"  SKIP  — headscale unreachable ({str(e)[:80]})")
        return gate_skip("remote_access", "headscale host unreachable")
    if ok is None:
        print(f"  SKIP  — {problems[0] if problems else 'not evaluable'}")
        return gate_skip("remote_access", problems[0] if problems else "not evaluable")
    if not ok:
        for p in problems:
            print(f"  FAIL  — {p}")
        return gate_fail("remote_access", "; ".join(problems)[:200])
    print("  PASS  router serving every team subnet; policy carries this comp's grants")
    return gate_pass("remote_access",
                     f"{len(identifiers)} subnet(s) serving, policy scoped")
