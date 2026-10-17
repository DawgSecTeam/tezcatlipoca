"""Remote-access gate: the engine's tailnet wiring is live, serving the right subnets, and the
headscale policy carries this competition's scoped grants."""

import subprocess
import time

from estate import state as estate_state
from net import remote_access_ops
from verifier import context
from verifier.model import gate_fail, gate_pass, gate_skip
from estate import config

# tailscaled's rebind re-applies its ruleset and drops this rule; the timer re-adds it every 30s,
# so give the probe one full timer cycle before believing the rule is gone.
SNAT_SETTLE_S = 90
SNAT_RETRY_S = 15


def _read_state(comp_dir):
    return estate_state.load_flat(comp_dir)


def _identifiers(teams):
    return sorted({str(t["identifier"]) for t in (teams or {}).values()
                   if isinstance(t, dict) and t.get("identifier") is not None})


def check_remote_access(comp_dir, teams, ctx):
# GateResult. Feature off (or pre-feature comp) skips WITHOUT gating; unreachable headscale is SKIP, never a pass.
    print("\n  (headscale remote access)")
    state = _read_state(comp_dir)
    ra = state.get("remote_access") or {}
    if not ra.get("enabled"):
        print("  SKIP  — remote access not enabled for this deploy (Compfile "
              "remote_access 0, or the comp predates the feature)")
        return gate_skip("remote_access", "not enabled for this deploy", gating=False)

    identifiers = _identifiers(teams)
    server_url = config.active().headscale.url.rstrip("/")
    if not server_url:
        print("  FAIL  — TEZ_HEADSCALE_URL not set; cannot confirm the engine's "
              "control plane (the deploy required it)")
        return gate_fail("remote_access", "TEZ_HEADSCALE_URL unset")
    try:
        enrolled = remote_access_ops.engine_tailscale_enrolled(ctx, server_url)
    except RuntimeError as e:
        print(f"  SKIP  — engine SSH failed ({str(e)[:80]})")
        return gate_skip("remote_access", "engine unreachable")
    if not enrolled:
        print("  FAIL  — engine tailscale is not Running against "
              f"{server_url} (BackendState/control mismatch)")
        return gate_fail("remote_access", "engine not enrolled to the headscale control plane")
    print(f"  PASS  engine enrolled to {server_url}")

    # Sampled past one full re-assert cycle: a single sample caught an unchanged range clean then
    # failing minutes apart, while a genuinely absent rule stays absent through the whole window.
    snat_probe = "; ".join(
        f"sudo iptables -t nat -S POSTROUTING | grep -q 'd 192.168.{i}.0/24 -j SNAT' && "
        f"echo ok{i} || echo MISSING{i}" for i in identifiers)
    deadline = time.time() + SNAT_SETTLE_S
    while True:
        proc = context.ssh_to_engine(ctx, snat_probe)
        missing = [ln for ln in (proc.stdout or "").split() if ln.startswith("MISSING")]
        if proc.returncode == 0 and not missing:
            break
        if time.time() >= deadline:
            print(f"  FAIL  — tailnet->team SNAT lines absent for: "
                  f"{', '.join(m.split('MISSING')[1] for m in missing) or 'unknown (rc)'}"
                  f" (sampled for {SNAT_SETTLE_S}s)")
            return gate_fail("remote_access", "SNAT lines missing from the engine nat table")
        time.sleep(SNAT_RETRY_S)
    print(f"  PASS  SNAT present for {len(identifiers)} team subnet(s)")

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
