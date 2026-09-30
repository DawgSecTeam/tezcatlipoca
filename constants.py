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
}
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
}
# unrealircd-backdoor-container (rc=127: assumes docker on the box) has no pin var —
# it is a box-prerequisite gap, caught by the verify plant-coverage gate when the
# step fails, not by this table.

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
GOLDEN_IP_BASE = 240
GOLDEN_TAG = "tezcatlipoca-golden"

WINDOWS_ADMIN_USER = "Administrator"

NAKON_DIR = Path("vendor/nakon")
