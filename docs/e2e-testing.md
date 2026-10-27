# E2E testing

Running and triaging a full pipeline test (`create-competition.py` / `run-agent-scrim.py`). Open
issues: [known-issues.md](known-issues.md). Node ground truth:
[environment-facts.md](environment-facts.md). Practice runs need their own worktree
([AGENTS.md](../AGENTS.md#practice-runs-must-run-from-a-new-worktree)).

Rules: triage before touching anything; never redo phases 1–2 for a failure in 3–8 (resume);
always capture the deploy log.

## 0. Pre-deploy checks

Per AGENTS.md, each of these has burned a real deploy and is also a defect to fix in code, not a
permanent checklist item.

- **Template has a cloud-init drive**: a tagged template with no `ide*/scsi*/sata*` `cloudinit`
  entry clones into an unreachable box. Use the `-fix` variants
  ([environment-facts.md](environment-facts.md#templates)).
- **Config profile matches the node and has no dead vmid** (`proxmox.template_vm_id`). Set
  `TEZ_TEAM_IDENTIFIERS` (or `--team-identifiers`) explicitly: packet-profile identifiers and the
  built-in default block are not availability checks.
- **Free golden/engine slots, and red01's slot**: another session may have squatted a vmid; the
  red01 vmid/IP preflight refuses an occupied slot and an IP a running guest answers, so a stale
  red01 from another worktree surfaces before the deploy.
- **Distinct `engine.mgmt_ip` per concurrent comp**: `clean_engine_for_template` SSHes to the
  planned mgmt IP, so a shared IP can wipe a live foreign engine's `.env`, `event.conf`, and host
  keys.
- **Reclaim orphaned ranges** with `destroy-competition.py` from the matching comp dir, never an
  ad-hoc sweep.
- **Probe the API port, not ping**: `curl -k https://<node>:8006/` must return an HTTP code, not
  `000`. If a node went down, resume with `--from-phase`; state, snapshots and disks survive.
- **Pool headroom and snapshot capability**: the gate needs `datastore` free vs the comp's per-box
  `disk_gb` (`infra/resources.py`) — Windows boxes dominate. `TEZ_THIN_HEADROOM` relaxes the gate
  on thin pools. Only ZFS, LVM-thin, Ceph, qcow2-on-file snapshot.
- **Tear bridges down with `ifdown`, never `ip link del`.** If API network changes fail on a
  missing `vmbrNNN/brif/`, clear `/run/network/ifstatenew` and `ifreload -a`.
- **Commit deploy-path code before `--freeze`**: it refuses uncommitted deploy-path source
  (extensions per `engine/template_freeze.py`; run state like the state document or `.env` backups
  is ignored). `--unfreeze --confirm-unfreeze` is the only way back, so freeze last.
- **Don't pin a known-broken catalog config** (`constants.KNOWN_BROKEN_CONFIGS`).
- **Don't point boxes at their team's `dns*` box at clone time**: its bind9 is installed by nakon
  via apt, which needs working DNS.
- **Distinct scoreboard Display per pin per box**: duplicates are refused at generate time;
  `pins_registered` catches unregistered pins.
- **`box_username` must not be a legacy system account** (`common/utils.py:LEGACY_ACCOUNT_NAMES`);
  cloud-init adopts the shadow account and logins fail. `medic` is safe.
- **Local-model scrims**: don't raise local blue's context above the slot limit; opencode's base
  prompt is ~20k tokens. No `reasoning_effort` on a local endpoint.

## 1. Triage

1. What changed since the last green run? `git log --oneline <last-green>..HEAD`; comment-only
   changes don't count.
2. Match the signature below. Only unmatched failures get a debugging session, then a new row here.
3. A real regression is a commit pair (introduced-by → fixed-by).

| Signature | Cause | Recovery |
|---|---|---|
| Phase 4 `nakon deploy --strict` exit 1 with `FAILED` step lines | Broken vulndb row aborts the golden plant (the only strict pass; phases 6–7 are lenient and tally) | Trim-then-resume (§3) |
| HTTP 596, authed API hangs, node reboot, `lock = clone` surviving reboot | Clones saturating the datastore; concurrent external provisioners worsen it | Preflight headroom; no concurrent provisioners |
| apt 404 mid-deploy | Mirror rotation; self-heals after apt-daily | Retry later |
| Clone has no IPv4/APIPA, resolv.conf reverted | Cloud-init race (rarer on golden-based clones) | No auto-repair: surfaces as `wait_for_boxes_ssh` failing with `diagnose_unreachable_box` output; re-clone (`redeploy-competition.py --mode rebuild`) or resume |
| Domain Join "domain does not exist" right after promotion | dc01 still settling | Resume phase 7; the ADDS artifact skips re-promotion |
| Phase 2 engine bootstrap `rc=100`, apt archives lock; `apt-get: command not found` ×5 on Fedora/Alpine | First-boot `unattended-upgrades` respawns apt; Debian-assuming prep | Handled: units masked, all three locks polled, `command -v apt-get` guard |
| Fedora member: golden plant `Bad authentication type`; httpd/named DOWN; agent flaps | Fedora cloud image: SSH password auth off, `named` localhost-only, httpd hangs on `ServerName` DNS | `base-fedora44-fix` template + `fix_services_on_boxes` (`ServerName localhost`, `listen-on { any; }`) |
| Teardown hangs "Still destroying…" on a Windows DC | Graceful shutdown never completes with a dead agent | `kill -9 $(cat /var/run/qemu-server/<vmid>.pid)`; `qm stop` other DCs first |
| Scoreboard frozen; `/api/engine` stale `last_round.StartTime`, zero `current_round_time` | Round loop doesn't auto-resume after an engine reboot | `verify-competition.py --fix-round-loop` |
| Engine→box scoring times out through pfSense, or the clone won't boot | Raw-CIDR WAN rule silently dropped; host-side ZFS config write breaks the loader | `<network>lan</network>`; inject config guest-side only (§5) |
| Event-side: opencode cycle deaths, scoreboard `Forbidden`, red-evidence scp hangs | Harness/agent side | [rehearsal-gates.md](rehearsal-gates.md) |

## 2. Recovery toolbox

- **Resume**: `estate-state.json` holds `last_phase`, teams, secrets. `--from-phase N` reloads it
  and reuses credentials; without state it's a hard error. `N > last_phase + 1` is refused;
  `--force-from-phase` overrides for a known-stale checkpoint. Phase 8 sub-steps are flag-gated and
  `unpause_engine` isn't idempotent — don't resume "to be safe" past a completed phase 8.
- **Markers**: `.postclone-swept`, `.nakon-domain-<team>-adds.json` (+ live join probes).
- **Snapshots**: `SNAP_BASE`/`SNAP_READY` (`common/constants.py`) at end of phases 4 and 7. A
  failed snapshot is only a WARNING; check `list_snapshots` before relying on one. Modes:
  [usage-agents.md](usage-agents.md#redeploy-modes).
- **Verify** is the pass/fail gate for any deploy claim. An unwaived SKIP fails the run; triage a
  gate that never ran before assuming the range is broken.
- **Logs**: always capture (§4) — `run-agent-scrim.py` writes `<run_dir>/deploy.log` for every
  deploy, success included; a bare `create-competition.py` leaves console scrollback only, and the
  artifact folder keeps phase markers and timings, not stdout
  ([automated-test-artifacts.md](automated-test-artifacts.md)).
- **Deploy lock**: `acquire_deploy_lock` refuses a second driver on the same comp dir and prints
  `pgrep -af create-competition`; kill that PID (never `pkill -f`, which matches every session's
  command line), confirm it is gone, then retry.

### Cost map

Phases 1–2 are the expensive, dice-rolling part; 3–8 are mostly idempotent. The resume you have is
cheaper than the redeploy you're considering, and each full teardown rerolls the environmental
failures. `pytest tests/` covers helpers, not the pipeline ([tests.md](tests.md)).

| Phase | Typical cause | Cheapest recovery | Don't |
|---|---|---|---|
| 2 | Creator abort; "already exists" refusal; engine-template build | Mismatch: reclaim with `destroy-competition.py`, then deploy fresh. Clean abort: fix and `--from-phase 2` | Hand-create/destroy the component's resources |
| 3 | apt/compose timeout | `--from-phase 3` (idempotent) | Rebuild boxes for an engine timeout |
| 4 | Strict abort on a bad row; golden build/convert; create race | Trim → `--from-phase 4` | `--from-phase 1`. A partially *converted* golden set is fatal (destroy and redeploy) |
| 5 | Firewall bootstrap/cutover (no-op without `in_path`) | `--from-phase 5` | Skip it on a firewall lineup |
| 6 | Pre-domain pass (lenient: usually a tally); DNS/auth; fix_services | Trim → `--from-phase 6`; rerun once for transients | Assume it re-clones (cloning is phase 4) |
| 7 | Domains, final pass, red01 + seed, `tz-ready` | Trim → `--from-phase 7`; check `.nakon-domain-*-adds.json` exist | Expect re-clone |
| 8 | Seed/inject/unpause | `--from-phase 8` | Rerun past an unpaused engine |

## 3. Trim-then-resume

`generate_nakon_config` rebuilds `nakon-config.json` from `box_vulns.json` on every invocation,
including resumes, so trimming takes effect immediately.

1. Read the `FAILED` step names and their boxes from the log.
2. Transient (scp reset, one flaky apt) → rerun once. Deterministic (same row on multiple boxes;
   rc 1/2/127; missing payload) → trim.
3. Remove the row names from `competitions/<id>/box_vulns.json`. Record each trim: it's a vulndb
   bug.
4. Resume at the phase where nakon ran (4, 6 or 7). Trim before the first resume: every team runs
   the same rows.

Under `--strict`, pin density is risk; expect a full-catalog pin run to iterate.

## 4. Running a test

Preflight: attribute any deploy-path changes and note the commit as `<last-green>`; templates
exist and aren't known-broken; `nakon catalog check` passes (automatic on fresh deploys, run by
hand for repair resumes, which skip it); headroom per §0; nothing else cloning on the node
(including the workshop portal provisioner); `--plan-only` and actually read it; choose log
capture.

```bash
# full harness (deploy + verify + agents + evidence + teardown)
.venv/bin/python run-agent-scrim.py --competition <id> --new --teams 2 ...
# deploy only; detach per usage-agents.md for long runs
python3 -u create-competition.py --competition <id> --teams 2 --yes 2>&1 | tee deploy.log
```

On failure: capture the tail and phase; triage (§1); strict → §3; timeouts → known class or raise
and record; environmental → wait/repair and resume at the same phase; a leftover range means
destroy-then-deploy, since a `--from-phase 1` re-run refuses the leftovers too. **Max 2
repair-resume cycles**, then fix the roots and take one bounded attempt or a fresh deploy; a third
consecutive resume is a loop.

After: `verify-competition.py` (no deploy is green without it); new failure class → row in §1;
record trims; teardown unless the next step needs the range.

## 5. Multi-host, pfSense and red runs

**Hosts.** Full walkthrough: [multi-node.md](multi-node.md). Per-host values, token sources, and
live occupancy: `infrastructure-status.md` (gitignored, repo root). Never assume a vmid across
hosts. Two traps beyond the multi-node doc:

- Env files hold unquoted JSON/pubkeys that break `source`; parse them (`shlex.quote` per
  assignment) and `unset TEZ_SCORING_VMID` so a stale range value doesn't override the generated
  one — the environment beats `tezcatlipoca.toml`.
- Before a host reboot: boot is ext4/LVM (a suspended ZFS data pool can't block it); confirm the
  bad pool isn't in `zdb -C`; snapshot the running set (`/root/running-before-reboot.txt`) since
  only `onboot=1` VMs auto-start.

**In-path pfSense.** Per team: `engine → transit<id> (172.31.<id>.1/30) → pfSense WAN .2 / LAN
192.168.<id>.1 → boxes`, pure router, outbound NAT off.

- Never inject config host-side (importing the pfSense ZFS pool on Proxmox): the installed OpenZFS
  makes it unmountable by pfSense's loader (`mountroot error 22`). The pipeline SSHes into the
  `pfsense-provision` clone via the engine, replaces `config.xml`, reboots. Keep the pool name
  `pfSense` (hardcoded in the loader).
- The WAN pass rule must be `<network>lan</network>`, not a CIDR.
- Persistent engine cutover: one reboot with all transit NICs added, netplan MAC-matching each
  (`set-name: transit<id>`, `172.31.<id>.1/30`, route via `.2`), no `.1` on team NICs. NIC hotplug
  past the first is unreliable. Firewalls need `onboot=1`.

**Red: tlaloc on red01.** Design notes in [internals.md](internals.md#red-and-tlaloc); the scrim's
pre-T0 gates are the deploy's proof ([scrim-harness.md](scrim-harness.md)). **Every red failure
aborts the deploy — there is no degraded range.** Phase 7 stages the agent through the same
`plant_assume_breach` the scrim uses, so a scrim cannot stage a red the deploy would not. The C2
callback is the team gateway `192.168.<id>.1` (the engine's DNAT on an engine-hosted team, the
satellite jump's DNAT on a satellite team) — never the engine's transit address, which is
Quotient's own.
