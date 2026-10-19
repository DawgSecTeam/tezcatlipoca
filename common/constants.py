"""Central constants for vmid math, snapshots, budgets, naming, and the nakon path."""

import re

from pathlib import Path

# Resume and redeploy refuse state from any other version: the phase numbering is part of what the value means.
PIPELINE_VERSION = 3

MAX_BOXES_PER_TEAM = 10

MAX_TEAMS = 154

SCORING_ENGINE_VMID = 1000

# Static: the DHCP engine drifted mid-event; high in the /24, clear of red01 slots and the observed drift.
DEFAULT_ENGINE_MGMT_IP = "10.0.0.250"
DEFAULT_ENGINE_MGMT_GW = "10.0.0.1"

# Top-of-/24: lower addresses hide collisions from the agent scan; an incomplete scan keeps the .250 default.
ENGINE_MGMT_BAND = ("10.0.0.251", "10.0.0.254")

SNAP_BASE = "tz-base"
SNAP_READY = "tz-ready"

PER_MACHINE_NAKON_BUDGET = 2400

SLOW_SERVICES = ("splunk", "roundcube")

# quotient merges these over the base check; nakonops strips them: planted config and scored check differ.
PIN_CHECK_OVERRIDES = ("display", "port", "path", "scheme", "status", "credlist")

DISRUPTIVE_CONFIGS = {"resolv-conf-null-dns", "apt-sources-empty", "apt-hold-all-packages", "dpkg-broken-hold-state"}

# domain_ops injects these per team at phase 6; a bare plant would run dcpromo without the identity vars.
DOMAIN_INFRA_CONFIGS = {"ADDS", "Domain Join", "domain-join"}

# Re-asserted post-clone, before the domain pass: member joins authenticate over SSH.
PREDOMAIN_STAGE_CONFIGS = {
    "ssh-empty-passwords", "ssh-max-auth-retries-high", "ssh-password-auth",
    "ssh-root-login", "ssh-x11-forwarding", "sshd-config-weak",
    "sshd-force-sftp-broken-chroot", "ssh-backdoor-listener-2222",
    "writable-sudoers",
    # Cross-box IP in the app's SQL config: plants per machine post-clone, not on the golden.
    "airship-webapp",
}
FINAL_STAGE_CONFIGS = DISRUPTIVE_CONFIGS | {
    # Baked into the golden, masked boot targets leave every linked clone with no network.
    "systemd-system-masked",
    # Machine-specific /etc/hosts entry: post-clone only, never rides the golden disk.
    "hosts-redirect-linux",
    # Only do the right thing on a promoted DC: plant per team after the domain pass, in phase 6.
    "ad-color-fleet-win",
    "ad-dns-localhost",
    "guest-enabled-win",
    "Elevate Guest Account",
    "weak-password-policy-win",
}
# Shape-based, not enumerated: the catalog keeps adding ad-* configs that need a promoted DC.
DOMAIN_DEPENDENT_CONFIG_PREFIXES = ("ad-",)
DOMAIN_DEPENDENT_CONFIGS = {
    "gpo-persistence-win",
    "gpp-cpassword-win",
    # Needs the promoted DC's NTDS parameters; has no ad- prefix, so the prefix rule misses it.
    "ldap-signing-off-win",
}


def is_domain_dependent(name):
    return (name.startswith(DOMAIN_DEPENDENT_CONFIG_PREFIXES)
            or name in DOMAIN_DEPENDENT_CONFIGS)


# Account writers keep running through the phase-7 red seed and reset these modes — plant after the last one.
ACCOUNT_FILE_MODE_CONFIGS = {
    "bad-perms-userConfig",
    "passwd-world-readable",
    "writable-etc-group",
}

# Combined view: what a convergence sweep (redeploy rollback-base/rebuild) must plant.
POST_CLONE_CONFIGS = (PREDOMAIN_STAGE_CONFIGS | FINAL_STAGE_CONFIGS
                      | ACCOUNT_FILE_MODE_CONFIGS)

# Kinds beyond the catalog's own required_vars: "ip" = this machine's address, "ip:<box>" = the
# same team's <box>, "literal" = operator pin {"name", "vars"}; a bare name is a generate error.
REQUIRED_VARS = {
    "hosts-redirect-linux": {"IP": "ip", "HOSTS": "literal"},
    # The payload requires the uppercase var names; lowercase keys die rc=2.
    "sudoers-rule": {"DROPIN_NAME": "literal", "RULE": "literal"},
    "airship-webapp": {"DB_USER": "literal", "DB_PASS": "literal",
                       "DB_HOST": "ip:polus"},
    "raw-socket-beacon": {"SERVICE_NAME": "literal"},
    "desktop-autostart": {"DEST": "literal", "EXEC": "literal"},
}
# Live-confirmed catalog defects: pinning is a generate-time error; reasons per row; shrink as upstream fixes.
KNOWN_BROKEN_CONFIGS = {
    "mailenable-cleartext-mail-win":
        "Planted cleartext-mail finding never lands: `choco install mailenable` is a "
        "wrong package id (no such package in the community feed), so nothing installs and "
        "only the firewall rule is created.",
    "MSSQL":
        "Planted MSSQL service never comes up: the payload runs `Install-Module SqlServer` "
        "without bootstrapping the NuGet provider (Install-Module aborts with \"NuGet "
        "provider is required to interact with NuGet-based repositories\"), then "
        "`Invoke-Sqlcmd -ServerInstance \"YourServerName\"` against an unparameterized "
        "placeholder — the step exits rc=1 in ~23s on a fresh random selection, aborting "
        "phase 4.",
}

# Just below the golden block so one preflight scan covers both; per-competition, dies with a full teardown.
ENGINE_TEMPLATE_VMID_OFFSET = 140
ENGINE_TEMPLATE_NAME = "engine-template"

# Just above the engine's slot; team1-subnet IPs are free — goldens are gone before real team boxes come up.
GOLDEN_VMID_OFFSET = 150

# A 60G Windows clone measured >30 min on HDD-ZFS; the 1800s default aborted a healthy clone.
GOLDEN_CLONE_TIMEOUT = 5400
GOLDEN_IP_BASE = 240
GOLDEN_TAG = "tezcatlipoca-golden"

# .1/.10 repeat every range; on a shared L2 another range's engine answers for them — hence a per-run bridge.
RED_SEGMENT_SUBNET = "10.200.0.0/24"
RED_SEGMENT_GW = "10.200.0.1"
RED_SEGMENT_IP = "10.200.0.10"


def red_bridge_name(run_id):
    """This run's private red-segment bridge. PVE cap: 15 chars; `vmbrR` + 8 hex fits."""
    hex_id = "".join(c for c in (run_id or "").lower() if c in "0123456789abcdef")[:8]
    return f"vmbrR{hex_id}"

def ownership_tags(comp_name, run_id=None, *extra):
    # Every destruction guard requires this full set; the comp tag is shared by concurrent
    # runs of one competition, so a missing run tag means another run's VM — refuse.
    # Proxmox tags cannot carry a space: "CDE 2026" was written as two tags and the
    # teardown then refused its own engine (live: cde-2026, 2026-10-19).
    slug = re.sub(r"[^a-z0-9-]+", "-", str(comp_name).lower()).strip("-")
    tags = {"tezcatlipoca", f"comp-{slug}"}
    if run_id:
        tags.add(run_id)
    return tags | set(extra)

WINDOWS_ADMIN_USER = "Administrator"

NAKON_DIR = Path("vendor/nakon")
