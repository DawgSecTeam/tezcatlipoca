"""Central constants for vmid math, snapshots, budgets, naming, and the nakon path."""

from pathlib import Path

MAX_BOXES_PER_TEAM = 10

MAX_TEAMS = 154

SCORING_ENGINE_VMID = 1000

SNAP_BASE = "tz-base"
SNAP_READY = "tz-ready"

PER_MACHINE_NAKON_BUDGET = 2400

SLOW_SERVICES = ("splunk", "roundcube")

DISRUPTIVE_CONFIGS = {"resolv-conf-null-dns", "apt-sources-empty", "apt-hold-all-packages", "dpkg-broken-hold-state"}

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
