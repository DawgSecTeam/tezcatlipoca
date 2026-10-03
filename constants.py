"""Central constants for vmid math, snapshots, budgets, naming, and the nakon path."""

from pathlib import Path

MAX_BOXES_PER_TEAM = 10

MAX_TEAMS = 154

SCORING_ENGINE_VMID = 1000

# Engine mgmt IP, static by default (deploy.py): the DHCP engine rebooted onto a
# different address mid-event while terraform's saved output stayed stale
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
# service's base check config (event.conf); nakon_ops.py strips them from machine lists,
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

# M3.2 stage split, in THREE passes (see generate_stage_configs):
#
#   golden   — everything identity-free and non-disruptive/non-boot-hostile: planted once
#              on the golden set, rides the linked clone.
#   repair   — sshd/sudoers touchers, re-asserted post-clone in phase 5 BEFORE the domain
#              pass: nakon's member joins authenticate over SSH, so a clone whose
#              sshd/sudoers state drifted through the clone's fresh cloud-init pass must
#              be repaired while the box is still joinable. fix_services_on_boxes runs
#              right after this pass to un-wedge the sshd the ssh-* configs thrash.
#   final    — disruptive (break DNS/apt) + boot-hostile configs, planted AFTER the
#              domain pass: phase 6's Domain Join / domain-join REBOOT member boxes
#              (boot-hostile configs planted earlier would brick the rebooting member),
#              and Linux realmd joins need working DNS + apt for the realmd install
#              (plausibly the long-documented "Linux member joins blow their timeouts"
#              flakiness — v1's plant ordering carried the same interaction). This is the
#              as-started competition flavor, so it lands right before beacons/tz-ready.
#
# Within each pass, DISRUPTIVE_CONFIGS still sort last per machine.
REPAIR_STAGE_CONFIGS = {
    "ssh-empty-passwords", "ssh-max-auth-retries-high", "ssh-password-auth",
    "ssh-root-login", "ssh-x11-forwarding", "sshd-config-weak",
    "sshd-force-sftp-broken-chroot", "ssh-backdoor-listener-2222",
    "writable-sudoers",
    # Cross-box identity (amongus-cde-2026): airship-webapp writes the app's SQL
    # config with DB_HOST = its team's polus address (REQUIRED_VARS "ip:polus"), so
    # it must plant per machine post-clone — apache itself rides the golden disk.
    "airship-webapp",
}
FINAL_STAGE_CONFIGS = DISRUPTIVE_CONFIGS | {
    # Boot-hostile (live-found 2026-09-24: baked into the golden disk, it left every
    # linked clone bootless — masked multi-user/graphical/default targets mean systemd
    # boots with no multi-user.target, so no cloud-init and no network). Catalog audit
    # (2026-09-24, all 179 linux misconfigs grepped for target/mask/fstab/initramfs/
    # kernel tampering): this is the only config that damages boot itself.
    "systemd-system-masked",
    # Identity-dependent (see REQUIRED_VARS): writes a machine-specific /etc/hosts
    # entry, so it must plant per machine post-clone, never ride the golden disk.
    "hosts-redirect-linux",
    # AD-dependent / domain-identity Windows configs (amongus-cde-2026): each only does
    # the right thing on a PROMOTED DC (AD module present, policy now domain-scoped,
    # zone DNSRoot resolvable). The DC's per-team pass for its otherwise golden-stage
    # configs still runs pre-promotion (repair stage), so these plant per team after
    # the domain pass in phase 6 instead. Members never pin them.
    "ad-color-fleet-win",
    "ad-dns-localhost",
    "guest-enabled-win",
    "Elevate Guest Account",
    "weak-password-policy-win",
}
# AD-dependent catalog configs by SHAPE, not enumeration: the cde-2026-era catalog
# grew a whole family of `ad-*` Windows misconfigs plus GPO/GPP ones that only do the
# right thing on a PROMOTED DC (scrim-one 2026-10-03: seven of them rode a DC's
# repair-stage plant pre-promotion, failed rc=1, and the range silently lost its AD
# attack surface — invisible until the plant_coverage gate existed). Everything
# `ad-*` and these GPO/GPP names plants per team in the final stage, post-domains.
DOMAIN_DEPENDENT_CONFIG_PREFIXES = ("ad-",)
DOMAIN_DEPENDENT_CONFIGS = {
    "gpo-persistence-win",
    "gpp-cpassword-win",
}


def is_domain_dependent(name):
    """True when a config needs a promoted DC and must plant in the final stage."""
    return (name.startswith(DOMAIN_DEPENDENT_CONFIG_PREFIXES)
            or name in DOMAIN_DEPENDENT_CONFIGS)


# Combined view: what a convergence sweep (redeploy rollback-base/rebuild) must plant.
POST_CLONE_CONFIGS = REPAIR_STAGE_CONFIGS | FINAL_STAGE_CONFIGS

# Pin-var requirements (M4 pin fix). The catalog DB carries no required-vars metadata
# (only depends_on), so this curated table is the operator-level source of truth:
# a bare-name selection of one of these configs is rejected at generate time instead
# of failing mid-plant (live-found 2026-09-24: sudoers-rule rc=2, hosts-redirect-linux
# rc=2, unrealircd rc=127). Var kinds:
#   "ip"      — machine-identity: auto-filled with that machine's own IP at stage-file
#               generation, and ONLY in the repair/final stages. A golden-stage plant
#               would bake the golden box's IP into every linked clone (generate
#               enforces this hard).
#   "ip:<box>"— cross-box identity: auto-filled with the SAME TEAM's copy of <box>'s
#               IP (e.g. a web box pointed at its team's database box). Same repair/
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
    # live-found 2026-09-25 (M4 validation run 1): the payload checks BOTH vars
    # uppercase via :?-required expansions — lowercase "rule" dies rc=2 in 0s.
    "sudoers-rule": {"DROPIN_NAME": "literal", "RULE": "literal"},
    # airship-webapp (amongus-cde-2026) writes the app's SQL connection config; DB_HOST
    # is the DATABASE box's address on the same team subnet, not the web box's own.
    "airship-webapp": {"DB_USER": "literal", "DB_PASS": "literal",
                       "DB_HOST": "ip:polus"},
}
# Catalog configs that must never be *newly* pinned: each is a live-confirmed defect in the
# shared vulndb catalog that takes a scored service down until it is fixed upstream. Pinning
# one is a generate-time error (nakon_ops._validate_known_broken_pins, packet_ops' compiler)
# instead of a mid-plant surprise. Pin sets used to carry this as prose + per-competition JSON
# pruning, so a new comp could silently re-pin them. Reasons: docs/known-issues.md and
# docs/upstream-defects-handoff.md; shrink the table as upstream fixes land.
#
# History: the four Linux rows (tftpd-hpa-anon-write, postgresql-no-auth,
# postgresql-remote-access, sshd-force-sftp-broken-chroot) were fixed and verified live on
# noble/debian13/fedora44 on 2026-10-02 and removed from this table — bodies and evidence in
# docs/vulndb-fixes/. The five Windows rows it used to carry were NOT user-policy defects at
# all: four of them never touch an account or password policy, and the fifth already created
# its account first and re-ran clean (measured live 2026-10-02 on lab vmid 131 — the whole
# "Set-LocalUser/password-policy ordering" diagnosis in upstream-defects-handoff §5 was wrong).
# Those four are fixed and removed below. mailenable-cleartext-mail-win is the one real
# Windows defect found and is STILL BROKEN: `choco install mailenable` names a package the
# community feed does not have, so the row plants no mail service at all.
KNOWN_BROKEN_CONFIGS = {
    "mailenable-cleartext-mail-win":
        "Planted cleartext-mail finding never lands: `choco install mailenable` is a "
        "wrong package id (no such package in the community feed), so nothing installs and "
        "only the firewall rule is created. Fix candidate (unverified) in "
        "docs/vulndb-fixes/mailenable-cleartext-mail-win.candidate.",
}
# NOT in KNOWN_BROKEN_CONFIGS: unrealircd-backdoor-container. Its rc=127 (docker absent) was
# fixed 2026-10-02 — the body now installs/detects docker or podman and otherwise exits rc=1
# with a MISSING DEPENDENCY message — so it is a normal pin again; verify's plant-coverage
# gate still catches a failed step. See docs/vulndb-fixes/unrealircd-backdoor-container-linux.sh.
#
# Also removed 2026-10-02 after live verification on lab vmid 131 (tz-vulnlab-w1), bodies and
# evidence in docs/vulndb-fixes/:
#   local-user-win — account was already created before its flags; the real bug was
#     `PASSWORD_NEVER_EXPIRES -eq 1` comparing nakon's string "1" to an integer, so the flag
#     silently no-opped.
#   powershell-execution-unrestricted — `Set-ExecutionPolicy -Scope LocalMachine` raised a
#     terminating SecurityException under nakon's Process-scope Bypass, so the step scored rc=1
#     and the registry writes below it never ran (no account or password policy involved).
#   rpc-proxy-on-dc-web-win — no account involved; RSAT-Rpc-Proxy is not a Server feature and
#     `poolManagementMode` is not an IIS property, so the plant was dirty but landed.
#   unauth-kiosk-app-startup-win — no account involved; worked as intended where its declared
#     app tree existed (0.0.0.0:80, HTTP 200, rc=0 twice); now reports a missing interpreter
#     explicitly instead of a bare Start-Process argument error.

# M4 per-competition templates: the engine template sits just below the golden block
# so one preflight scan covers both. Not a cross-competition cache: every template
# belongs to exactly one competition and dies with it (--full teardown).
ENGINE_TEMPLATE_VMID_OFFSET = 140
ENGINE_TEMPLATE_NAME = "engine-template"

# M3.1 golden set: one planted, template-converted VM per box type, linked-cloned to
# every team. vmids sit just above the engine's slot (engine_vmid + 150 + box_index);
# IPs sit above the .1 gateway and below .255 on team1's subnet (192.168.<team1 id>.<240+i>),
# free because golden boxes are long gone by the time real team boxes come up.
GOLDEN_VMID_OFFSET = 150

# Full 60G Windows-template clones land on whatever pool the node offers; on
# HDD-ZFS the same template has measured 13 min and >30 min (live-found 2026-09-30,
# cyberfield hdrives-zfs) — the 1800s task-wait default aborted a healthy clone.
GOLDEN_CLONE_TIMEOUT = 5400
GOLDEN_IP_BASE = 240
GOLDEN_TAG = "tezcatlipoca-golden"

def ownership_tags(comp_name, run_id=None, *extra):
    """The full ownership tag set for a competition's VMs — every destruction guard
    requires ALL of it on a VM before touching it.

    `run_id` is the per-deploy identity (utils.mint_run_id, persisted in
    .deploy_state.json): the comp tag alone is shared by every worktree running the
    same competition ID, so two concurrent deploys were indistinguishable to every
    sweep (2026-10-02 near-miss — smoke vs live-2box). A VM tagged comp-<name> but
    missing the expected run tag belongs to a DIFFERENT run and must be refused."""
    tags = {"tezcatlipoca", f"comp-{comp_name}"}
    if run_id:
        tags.add(run_id)
    return tags | set(extra)

# How many times the SAME phase may fail with the SAME signature before a resume at
# that phase is refused. `docs/e2e-testing.md` has always said "max 2 repair-resume
# cycles; a third consecutive resume is not a repair, it's a resume-loop", but that
# lived only in prose: cde-2026 burned eleven attempts (deploy6 -> deploy16,
# 2026-09-29/30) on one Windows golden. A DIFFERENT failure at the same phase resets
# the budget — that is new information, not a loop. `--force-from-phase` still overrides.
RESUME_ATTEMPT_LIMIT = 2

# `--min-load-free N`: on a resume into a phase that previously failed, wait for the
# node's 1-minute load to drop below N before starting. Operators hand-throttled this
# by eye ("load=32.06 (attempt 1/36) ... load below 10 - launching phase-4 resume",
# 2026-09-30); raising the sysprep timeout 900s -> 1800s did not help, the load did.
MIN_LOAD_FREE_TIMEOUT = 3600

WINDOWS_ADMIN_USER = "Administrator"

NAKON_DIR = Path("vendor/nakon")
