"""Headcale remote-access preflight: the env contract is present, the headscale host
is reachable, and no OTHER node already advertises a subnet this deploy will claim.

Runs before any Proxmox mutation (remote access ships with every deploy, so a
dead headscale host should stop a deploy at the gate, not mid-phase-3)."""

import remote_access_ops


def remote_access_gate(plan):
    """Raise SystemExit on the three refusal conditions; silent pass otherwise."""
    identifiers = sorted({str(t["identifier"])
                          for share in plan.shares
                          for t in (share.teams or {}).values()
                          if isinstance(t, dict) and t.get("identifier") is not None})
    if not identifiers:
        return
    remote_access_ops._headscale_env()  # SystemExit naming the missing vars
    if not remote_access_ops.headscale_reachable():
        raise SystemExit(
            "  ERROR: headscale is unreachable over ssh "
            f"({remote_access_ops._headscale_env()['TEZ_HEADSCALE_SSH_USER']}@"
            f"{remote_access_ops._headscale_env()['TEZ_HEADSCALE_SSH_HOST']}). Remote "
            "access ships with every deploy, so the headscale host must be reachable "
            "before the deploy touches Proxmox. Fix TEZ_HEADSCALE_* in .env or set "
            "`remote_access 0` in the Compfile to opt out.")
    conflicts = remote_access_ops.route_conflicts(plan.comp_name, identifiers)
    if conflicts:
        raise SystemExit(
            "  ERROR: headscale already has live subnet route(s) overlapping this "
            "competition's team subnets — two routers advertising the same /24 are one "
            "HA route group and would cross-route each other's participants:\n    "
            + "\n    ".join(conflicts)
            + "\n  Tear down the other range first, or deploy with disjoint team "
              "identifiers (TF_VAR_team_identifiers).")
