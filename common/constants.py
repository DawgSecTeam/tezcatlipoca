"""Central constants for vmid math, snapshots, budgets, naming, and the nakon path."""

from pathlib import Path

# Stamped into the state document by the deploy; resume and redeploy refuse state
# written by any other value (the phase numbering is part of what the number means).
PIPELINE_VERSION = 3

MAX_BOXES_PER_TEAM = 10

MAX_TEAMS = 154

SCORING_ENGINE_VMID = 1000

# Engine mgmt IP, static by default (deploy.py): the DHCP engine rebooted onto a
# different address mid-event while the recorded one stayed stale
# (shakedown-5x4: .221→.243→.233). High in the lab's 10.0.0.0/24 mgmt range, clear
# of the red01 slots (.198/.199/.244) and the observed engine drift (.22x/.23x).
# The preflight sweeps running guests' agent-reported IPs to refuse a collision.
DEFAULT_ENGINE_MGMT_IP = "10.0.0.250"
DEFAULT_ENGINE_MGMT_GW = "10.0.0.1"

SNAP_BASE = "tz-base"
SNAP_READY = "tz-ready"

PER_MACHINE_NAKON_BUDGET = 2400

SLOW_SERVICES = ("splunk", "roundcube")

# Per-pin scoring overrides for dict-form box_services.json pins, e.g.
# {"name": "IIS HTTP", "display": "iis-alt"}. quotient/setup.py merges them over the
# service's base check config (event.conf); nakonops/nakon_config_ops.py strips them from machine lists,
# so nakon plants the catalog config once while the SCORED CHECK differs. Quotient
# requires unique <box>-<Display> check names — display is how same-TYPE pins coexist.
# "credlist" swaps which credlist file the check authenticates with (packet dual-credit:
# a domain-account twin check scores the domain dimension of a service).
PIN_CHECK_OVERRIDES = ("display", "port", "path", "scheme", "status", "credlist")

DISRUPTIVE_CONFIGS = {"resolv-conf-null-dns", "apt-sources-empty", "apt-hold-all-packages", "dpkg-broken-hold-state"}

# Domain infrastructure is scored (ADDS maps to a Quotient Tcp check) but never
# nakon-planted from box_services.json: domain_ops.injects ADDS/Domain Join/domain-join
# per team at phase 6 with the per-team domain/credential vars. A bare ADDS riding the
# golden-stage machine list would run dcpromo without those vars.
DOMAIN_INFRA_CONFIGS = {"ADDS", "Domain Join", "domain-join"}

# M3.2 stage split, in FOUR passes (see generate_stage_configs):
#
#   golden   — everything identity-free and non-disruptive/non-boot-hostile: planted once
#              on the golden set, rides the linked clone.
#   pre-domain — sshd/sudoers touchers, re-asserted post-clone in phase 6 BEFORE the domain
#              pass: nakon's member joins authenticate over SSH, so a clone whose
#              sshd/sudoers state drifted through the clone's fresh cloud-init pass must
#              be repaired while the box is still joinable. fix_services_on_boxes runs
#              right after this pass to un-wedge the sshd the ssh-* configs thrash.
#   final    — disruptive (break DNS/apt) + boot-hostile configs, planted AFTER the
#              domain pass: phase 6's Domain Join / domain-join REBOOT member boxes
#              (boot-hostile configs planted earlier would brick the rebooting member),
#              and Linux realmd joins need working DNS + apt for the realmd install
#              (plausibly the long-documented "Linux member joins blow their timeouts"
#              flakiness). This is the
#              as-started competition flavor, so it lands right before beacons/tz-ready.
#   last     — account-file MODES, planted after the last account writer of the deploy
#              (bad-auto's assume-breach seed) and before the trail wipe; see
#              ACCOUNT_FILE_MODE_CONFIGS for why no earlier pass can hold them.
#
# Within each pass, DISRUPTIVE_CONFIGS still sort last per machine.
PREDOMAIN_STAGE_CONFIGS = {
    "ssh-empty-passwords", "ssh-max-auth-retries-high", "ssh-password-auth",
    "ssh-root-login", "ssh-x11-forwarding", "sshd-config-weak",
    "sshd-force-sftp-broken-chroot", "ssh-backdoor-listener-2222",
    "writable-sudoers",
    # Cross-box identity: airship-webapp writes the app's SQL config with
    # DB_HOST = another box's address (REQUIRED_VARS "ip:polus"), so it must
    # plant per machine post-clone — apache itself rides the golden disk.
    "airship-webapp",
}
FINAL_STAGE_CONFIGS = DISRUPTIVE_CONFIGS | {
    # Boot-hostile (a live deploy caught it: baked into the golden disk, it left
    # every linked clone bootless — masked multi-user/graphical/default targets
    # mean systemd boots with no multi-user.target, so no cloud-init and no
    # network). Catalog audit: this is the only config that damages boot itself.
    "systemd-system-masked",
    # Identity-dependent (see REQUIRED_VARS): writes a machine-specific /etc/hosts
    # entry, so it must plant per machine post-clone, never ride the golden disk.
    "hosts-redirect-linux",
    # AD-dependent / domain-identity Windows configs: each only does
    # the right thing on a PROMOTED DC (AD module present, policy now domain-scoped,
    # zone DNSRoot resolvable). The DC's per-team pass for its otherwise golden-stage
    # configs still runs pre-promotion (pre-domain stage), so these plant per team after
    # the domain pass in phase 6 instead. Members never pin them.
    "ad-color-fleet-win",
    "ad-dns-localhost",
    "guest-enabled-win",
    "Elevate Guest Account",
    "weak-password-policy-win",
}
# AD-dependent catalog configs by SHAPE, not enumeration: the catalog carries a whole
# family of `ad-*` Windows misconfigs plus GPO/GPP ones that only do the right thing
# on a PROMOTED DC (caught on a live run: seven of them rode a DC's pre-domain plant
# pre-promotion, failed rc=1, and the range silently lost its AD attack surface —
# invisible until the plant_coverage gate existed). Everything `ad-*` and these
# GPO/GPP names plants per team in the final stage, post-domains.
DOMAIN_DEPENDENT_CONFIG_PREFIXES = ("ad-",)
DOMAIN_DEPENDENT_CONFIGS = {
    "gpo-persistence-win",
    "gpp-cpassword-win",
    # ldap-signing-off-win seeds the DC's NTDS LDAP-signing parameters, which only exist
    # once a DC is promoted — run in the phase-6 pre-domain pass it dies rc=1 with
    # "NTDS Parameters key not found -- this seed only applies to a domain controller"
    # (a live DC run showed it; the name has no `ad-` prefix so the
    # prefix rule missed it).
    "ldap-signing-off-win",
}


def is_domain_dependent(name):
# True when a config needs a promoted DC and must plant in the final stage.
    return (name.startswith(DOMAIN_DEPENDENT_CONFIG_PREFIXES)
            or name in DOMAIN_DEPENDENT_CONFIGS)


# Account-file MODES plant in the deploy's LAST pass, not the golden or post-clone passes.
#
#     shadow-utils' commonio (useradd/groupadd/usermod) recreates /etc/passwd, /etc/shadow
#     and /etc/group at their default modes, discarding a planted mode; chpasswd alone
#     preserves it (probed in the Debian 13 and Ubuntu 24.04 base images:
#     666 -> 664 on useradd, unchanged on chpasswd). Two account writers run after the
#     pre-domain and final passes — `guests.service_fixup_ops.fix_services_on_boxes`
#     (a credlist `useradd` for every team box, end of phase 6) and bad-auto's assume-breach
#     seed (its depth-3 persistence `user` method, phase 7). caught live:
#     bad-perms-userConfig read ABSENT on every team clone while writable-sudoers — a
#     DIRECTORY mode neither writer touches — was present on all of them, and
#     plant_coverage PASSed because the earlier plant genuinely applied.
#     nakon_config_ops stages these after the seed and before the trail wipe.
ACCOUNT_FILE_MODE_CONFIGS = {
    "bad-perms-userConfig",     # chmod 666 /etc/passwd /etc/shadow /etc/crontab
    "passwd-world-readable",    # chmod 666 /etc/passwd
    "writable-etc-group",       # chmod 666 /etc/group
}

# Combined view: what a convergence sweep (redeploy rollback-base/rebuild) must plant.
POST_CLONE_CONFIGS = (PREDOMAIN_STAGE_CONFIGS | FINAL_STAGE_CONFIGS
                      | ACCOUNT_FILE_MODE_CONFIGS)

# Pin-var requirements (M4 pin fix). The catalog DB carries no required-vars metadata
# (only depends_on), so this curated table is the operator-level source of truth:
# a bare-name selection of one of these configs is rejected at generate time instead
# of failing mid-plant (caught live: sudoers-rule rc=2, hosts-redirect-linux
# rc=2, unrealircd rc=127). Var kinds:
#   "ip"      — machine-identity: auto-filled with that machine's own IP at stage-file
#               generation, and ONLY in the pre-domain/final stages. A golden-stage plant
#               would bake the golden box's IP into every linked clone (generate
#               enforces this hard).
#   "ip:<box>"— cross-box identity: auto-filled with the SAME TEAM's copy of <box>'s
#               IP (e.g. a web box pointed at its team's database box). Same pre-domain/
#               final-stage-only rule as "ip"; the target box name must exist in
#               boxes.json or generate fails loudly.
#   "literal" — operator-supplied: pin as {"name": ..., "vars": {...}} in
#               box_services.json / box_vulns.json; a bare name is a generate error.
# Catalog-side resolution is deferred to upstream nakon/vulndb (see known-issues).
REQUIRED_VARS = {
    # writes "IP  HOSTS" into /etc/hosts: IP = the machine's own address
    # (identity — auto-filled per machine, post-clone only), HOSTS = the
    # hostname(s) the operator wants that box to resolve to itself (literal).
    "hosts-redirect-linux": {"IP": "ip", "HOSTS": "literal"},
    # The payload checks BOTH vars uppercase via :?-required expansions —
    # lowercase "rule" dies rc=2 in 0s.
    "sudoers-rule": {"DROPIN_NAME": "literal", "RULE": "literal"},
    # airship-webapp writes the app's SQL connection config; DB_HOST
    # is the DATABASE box's address on the same team subnet, not the web box's own.
    "airship-webapp": {"DB_USER": "literal", "DB_PASS": "literal",
                       "DB_HOST": "ip:polus"},
    # Random-selection guard: these carry catalog `required_vars` but were
    # absent from this table, so `nakon randomize` returned them as BARE names on a fresh
    # competition, the generate-time var gate had nothing to reject, and the nakon bundle
    # lint aborted the whole deploy with "catalog payload(s) reference variable(s) their
    # step does not declare" (raw-socket-beacon: SERVICE_NAME; desktop-autostart:
    # DEST, EXEC). Listing them here makes _bare_unplantable_reason drop them from fresh
    # random selections and forces an explicit {"name": ..., "vars": {...}} pin.
    # NOTE: this curated table is a known-incomplete mirror of the catalog's own
    # required_vars metadata; the fresh-selection drop is now ALSO driven from that
    # metadata (catalog_required_var_names), so this table only carries the var KINDS.
    "raw-socket-beacon": {"SERVICE_NAME": "literal"},
    "desktop-autostart": {"DEST": "literal", "EXEC": "literal"},
}
# Catalog configs that must never be *newly* pinned: each is a live-confirmed defect in the
# shared vulndb catalog that takes a scored service down until it is fixed upstream. Pinning
# one is a generate-time error (nakon_pin_ops._validate_known_broken_pins, packet_ops' compiler)
# instead of a mid-plant surprise. Pin sets used to carry this as prose + per-competition JSON
# pruning, so a new comp could silently re-pin them. The reason string is on each row; shrink
# the table as upstream fixes land.
#
# mailenable-cleartext-mail-win is STILL BROKEN: `choco install mailenable` names a package the
# community feed does not have, so the row plants no mail service at all.
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

# M4 per-competition templates: the engine template sits just below the golden block
# so one preflight scan covers both. Not a cross-competition cache: every template
# belongs to exactly one competition and dies with it (full teardown).
ENGINE_TEMPLATE_VMID_OFFSET = 140
ENGINE_TEMPLATE_NAME = "engine-template"

# M3.1 golden set: one planted, template-converted VM per box type, linked-cloned to
# every team. vmids sit just above the engine's slot (engine_vmid + 150 + box_index);
# IPs sit above the .1 gateway and below .255 on team1's subnet (192.168.<team1 id>.<240+i>),
# free because golden boxes are long gone by the time real team boxes come up.
GOLDEN_VMID_OFFSET = 150

# Full 60G Windows-template clones land on whatever pool the node offers; on
# The same template has measured 13 min and >30 min on a HDD-ZFS pool —
# the 1800s task-wait default aborted a healthy clone.
GOLDEN_CLONE_TIMEOUT = 5400
GOLDEN_IP_BASE = 240
GOLDEN_TAG = "tezcatlipoca-golden"

# The routed-red segment red01 and its engine share. It rides its OWN per-run bridge
# (red_bridge_name), never the management bridge: the segment's gateway `.1` and red01's
# `.10` are the same two addresses in every range, so on a shared L2 the second concurrent
# range's engine answers red01's ARP for `.1` and red's traffic to its teams goes to a range
# that has no route to them — total "unreachable over SSH" (caught live: three campaign
# ranges plus a fourth session's red01 all held 10.200.0.1/24 on vmbr0). A private bridge
# keeps the same addresses unique because the L2 itself is per-run.
RED_SEGMENT_SUBNET = "10.200.0.0/24"
RED_SEGMENT_GW = "10.200.0.1"
RED_SEGMENT_IP = "10.200.0.10"


def red_bridge_name(run_id):
    """This run's private red-segment bridge. PVE cap: 15 chars; `vmbrR` + 8 hex fits."""
    hex_id = "".join(c for c in (run_id or "").lower() if c in "0123456789abcdef")[:8]
    return f"vmbrR{hex_id}"

def ownership_tags(comp_name, run_id=None, *extra):
# The full ownership tag set for a competition's VMs — every destruction guard
#     requires ALL of it on a VM before touching it.
#
#     `run_id` is the per-deploy identity (utils.mint_run_id, persisted in
#     the state document): the comp tag alone is shared by every worktree running the
#     same competition ID, so two concurrent deploys were indistinguishable to every
#     sweep (a near-miss between a smoke range and another comp). A VM tagged comp-<name> but
#     missing the expected run tag belongs to a DIFFERENT run and must be refused.
    tags = {"tezcatlipoca", f"comp-{comp_name}"}
    if run_id:
        tags.add(run_id)
    return tags | set(extra)

WINDOWS_ADMIN_USER = "Administrator"

NAKON_DIR = Path("vendor/nakon")
