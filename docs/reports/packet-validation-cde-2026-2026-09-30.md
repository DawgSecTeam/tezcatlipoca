# packet-profile validation — cde-2026 on cyberrange/.150 (2026-09-29/30)

The first packet-compiled deploy (see [packet-profiles.md](../packet-profiles.md)) surfaced nine
pipeline failures, all fixed in the same commit. Recorded here with the shapes that produced them,
since each is reachable without the packet layer; [known-issues.md](../known-issues.md) keeps only
the standing rules that came out of it.

The first packet-compiled deploy (see docs/packet-profiles.md) surfaced nine pipeline
failures, all fixed in the same commit; recorded here with the shapes that produced
them, since each is reachable without the packet layer:

- **Unmanaged boxes vs the M4 golden-hash loop** — the first bundle ever carrying an
  `unmanaged: true` box through phase 4 KeyError'd (`golden_machines_by_box['fw01']`):
  every prior pfsense comp carried its firewall outside boxes.json. Unmanaged slots
  now get stable hash entries like unbooted DCs.
- **Static engine mgmt IP skips the gateway default** — setting
  `TF_VAR_engine_mgmt_ip` explicitly skipped the branch that defaulted
  `TF_VAR_engine_mgmt_gw`, so the engine-template build PUT `ipconfig0` with an empty
  `gw=` → PVE 400 "Parameter verification failed" mid-phase-2. The gw now defaults
  whenever the IP is static.
- **Engine-template leftover adoption** — a config-PUT failure between clone and tag
  PUT leaves a leftover wearing the BASE image's tags (`cloud-init;template`), which
  the ownership guard then refused forever. Unconverted VMs on the reserved slot with
  the reserved name are adopted (loudly); converted templates keep the strict check.
- **Team identifiers vs engine-derived vmid slots** — engine 1080 + default
  identifiers 101/102 put team2's first box vmid exactly on the engine-template slot
  (engine+140). `collect_teams` now refuses any identifier whose full 10-slot block
  overlaps the engine, engine-template, or golden block — the old check covered the
  engine only, and this collision surfaces hours later at apply #2.
- **Fedora cloud btrfs root breaks root-disk expansion** — `findmnt -no SOURCE /`
  reports `/dev/sda3[/root]`; the bracketed suffix broke the partition-digit parse AND
  resize2fs can't grow btrfs. Expansion now strips the suffix, grows btrfs natively,
  and tolerates late `/dev/dm-N` (`dmsetup mknodes`) — the base-ubuntu24.04-fix golden
  ran minutes with the LV mounted but no device node. Failures retry, then fail hard
  only when the root fs is measurably too small (regression-4x1 shape); unmeasurable
  warns and continues (the boot-window instability that broke the grow breaks the
  probe too).
- **Windows account caps vs packet credentials** — `New-LocalUser -Description` caps
  at 48 chars (54-57-char decoy descriptions died in ParameterBindingValidation), and
  local-account creation enforces min length 8 (`airship`/`airship` →
  InvalidPasswordException while `n0t_sus1` passed). The compiler shortens baseline
  descriptions and filters sub-8-char credlist passwords out of Windows baseline pins.
- **Swallowed domain-chain failures** — `deploy_domain_configs` discarded
  `run_concurrent` results; a team chain that raised right after ADDS promotion left
  the AD misconfig pass, packet accounts, and member joins silently unplantable (only
  an hour-later verify domains FAIL revealed it). Failures are now aggregated and fail
  the phase; `--from-phase 6` re-enters safely (promotion probes + markers).
- **IIS FTP enforces SSL** — the planted site answers `534 Policy requires SSL`, so
  the engine's plain-FTP check can never pass. The CDE profile plants the site
  (`plant_only` pin) and scores port 21 with a score-only Tcp check.
- **Windows golden first-boot vs node contention** — with a second deploy saturating
  .150 (load ~20), a fresh sysprep-specialize first boot exceeded 900s AND 1800s.
  Golden Windows bootstrap timeout raised to 1800s; under contention, wait the load
  out before rebuilding Windows goldens rather than looping.
