# `docs/known-issues.md` triage — 2026-10-02

Audit of [known-issues.md](known-issues.md) (831 lines, 69 entries): why each entry is in the file,
what an actual fix would be, and whether it is worth doing. This is a docs/analysis artifact — no code
was changed for it.

**Method.** Each claim was checked against the working tree (code, tests, git history/branches) or
against the two Proxmox nodes with **read-only** API calls on 2026-10-02. Live probes are marked
**[live]**; claims taken at face value are marked **[doc]**.

---

## 1. Why everything is in there

The file's own rationale — *"kept because the mitigation's rationale is the load-bearing part"* — is
true for the incident log and is genuinely valuable there. The 600 s engine bootstrap timeout only
makes sense next to the cold-start measurement; `-parallelism=1` only makes sense next to the
datastore saturation. Deleting those write-ups would delete the reasoning that keeps the workarounds
correct.

But that rationale only covers **one** of the five genres now sharing the file:

| Genre | Examples | Why it's really there | Does it belong in one "known issues" file? |
|---|---|---|---|
| Incident post-mortem (the stated purpose) | boot-hostile golden, `#`-in-DB-password, apt-lock race | mitigation rationale is load-bearing | yes, but once fixed it is history, not a live list |
| Operator trap / runbook | orphan `--scoring-vmid`, `.env` stale vars, freeze-not-commit-safe, ZFS injection | nobody wrote the runbook entry anywhere else | yes — this is the part that must stay short and be read before deploying |
| Upstream defect (not ours) | `tftpd-hpa-anon-write`, `postgresql-remote-access`, `sshd-force-sftp-broken-chroot`, `local-user` on Fedora, `unrealircd-backdoor-container`, winget/choco | the catalog lives in `vendor/nakon` + a separate vulndb; this file is the only local record | no — needs a machine-readable exclusion list, prose cannot gate a pin |
| Environmental / hardware fact | cyberfield hard-downs, site lab outage, thick LVM cannot snapshot | observed reality, nothing to fix | no — belongs in node/host docs |
| Security disclosure | the three audits | provenance + "what to do next time" | no — belongs in its own file with an owner |

The structural problem is the framing. Line 18 says *"Almost everything below is fixed or
mitigated"* and the Open issues section collects four entries. In reality **20 of the 69 entries are
open in substance**, and two unresolved ones are misfiled *inside* "Fixed incidents":

- **PAM account-stage SSH denial** (line 241) — explicitly *"root cause UNSOLVED"*.
- **`sshd-force-sftp-broken-chroot`** (line 407) — *"upstream vulndb script fix still pending"*.

So the doc simultaneously overstates how closed the list is, under-reports what is open, and buries
the actionable traps among 38 post-mortems. Nobody reads 831 lines before a deploy; that is the real
defect.

---

## 2. Live corrections to the doc (2026-10-02, [live])

These are places the doc is now wrong, or silent about something bigger than what it documents.

| Doc says | Reality today | Consequence |
|---|---|---|
| cyberrange open note: *"orphan `quotient-engine` runs at vmid 1000"* | vmid 1000 does not exist on .150 (`vmid 1000 not found on node proxmox`) | the specific trap is gone; the class is not — see next row |
| — (not documented anywhere) | **43 tezcatlipoca VMs from 3 past competitions are still on the nodes, 20 of them running** | see §3 A2 |
| update 2026-09-29: *"hdd has since recovered to ~900 GB free"* | hdd on .150 is **266 GiB free** of 2613 (ssd 383, wkshp-pool 755, local-lvm 16/16, local 145/212) | the "only wkshp-pool fits a 10-box comp" note is true again |
| 2026-10-01 env leak: *"token is permanently public until rotated"*; blob retained on the public remote | token in the leaked file is **byte-identical to the token in the live `.env`** and still authenticates (HTTP 200, .193). `origin/main` still reaches the blob (8cb755c) | the one security item with a live credential is not just un-removed, it is **un-rotated** |
| AGENTS.md warns about the stale `TF_VAR_template_vm_id=9106` in the realm-backup variant | that file is now 955 **[live, correct]**, but the **primary `.env` carries `TF_VAR_template_vm_id=9088`, which exists on neither .150 nor .193** | main-tree `.env` is itself a dead-vmid trap |
| `sshd-force-sftp-broken-chroot` *"dropped from both extreme pin sets"* | it is classified in `constants.REPAIR_STAGE_CONFIGS` (a stage map, not an exclusion); **no code anywhere excludes it, `tftpd-hpa-anon-write`, or `postgresql-remote-access`** — "pruned" lives only in per-competition JSON | a new competition can silently re-pin any of them |
| *"standing instrument"* pam-lab, bisect harness | no `scrim-runs/`, no pam-lab script, no `sshd2` harness anywhere in the tree | the only unsolved incident is now unreproducible from the repo |

Leftover ranges, precisely **[live]**:

| Node | Competition | VMs | Running | What survives of its state |
|---|---|---|---|---|
| .150 | `scale8-scrim-2026-10-01` | 16 (jump 2031, goldens 2060-2064, team 2450-2484) | 10 | branch `scale8-2026-10-01` + `.tez-backups/scale8-preserve/` |
| .193 | `cde-2026` | 14 (engine 1080, goldens 1230-1233, team 1400-1403 + 1410-1413) | 9 | `competitions/cde-2026/` on disk |
| .193 | `scale8-scrim-2026-10-01` | 7 (engine 1900, 2040, 2050-2054) | 1 | same branch/backup as above |
| .193 | `amongus-cde-2026` | 6 (1030, 1640, 1650-1653) | 0 | `competitions/amongus-cde-2026/` on disk |

`cde-2026` is plausibly intentional (its dir is live and its packet report is from 2026-09-30). The
other two are what AGENTS.md's practice-run rule exists to prevent: runs whose comp dir was discarded
while the infrastructure kept running. They also explain part of the hdd squeeze.

---

## 3. Triage

### A. Act now — current, real risk

**A1. Rotate the leaked Proxmox token; scrub the local plaintext copies.** [live]
The leaked `root@pam!agent` secret is the same string the live `.env` uses against .193 and it still
authenticates. History rewrite already happened locally; the remote still carries the blob
(`origin/main` = 8cb755c), and local branches have diverged by 237/177 commits since, so a force-push
is now expensive and disruptive. Rotation is the only cheap fix that actually closes it. Then delete
the three plaintext copies that still sit on disk: `.env.pre-cde-20260929`,
`.tez-backups/env.pre-cde-20260929.20261001-232202`, and the two pre-rewrite bundles
(`all-refs-20261001-232202.bundle`, `pre-rewrite-20261002-002405.bundle`) if the rewritten history
does not need them. **Worth it: yes — highest-value item in the file.**

**A2. Reclaim the orphaned ranges.** [live]
`destroy-competition.py` is the sanctioned tool (resumable, tag-scoped, refuses foreign VMs) and both
non-cde comps still have comp dirs — `amongus-cde-2026` on disk, `scale8-scrim-2026-10-01` on branch
`scale8-2026-10-01` and in `.tez-backups/scale8-preserve/`. Run destroy from a worktree of that branch
for scale8, and from main for amongus, then re-check the node lists. Confirm `cde-2026`'s intent before
touching it. **Worth it: yes — 20 running VMs, and it returns vmids and hdd headroom.**

**A3. Fix `.env`'s dead `TF_VAR_template_vm_id=9088` → 955.** [live]
Same class AGENTS.md already documents for the realm-backup variant, now in the primary env, and it
hard-fails the engine-base preflight. **Worth it: yes, one line.**

### B. Worth a cheap guard — closes a recurring class

| # | Item | Concrete fix | Worth it |
|---|---|---|---|
| B1 | Broken/pruned configs exist only as prose + per-comp JSON | `KNOWN_BROKEN_CONFIGS` (name → reason) in `constants.py`; generate-time error if a pin names one. Covers `tftpd-hpa-anon-write`, `postgresql-remote-access`, `sshd-force-sftp-broken-chroot`, the 5 Windows user-policy pins, `unrealircd-backdoor-container` (docker), winget/choco. | **Yes** — prose cannot gate a pin, and the current guard is a copy-paste into each comp's JSON |
| B2 | Round loop stays stopped after an engine reboot (the only labeled open item that is ours) | A watchdog container/timer on the engine that POSTs `competition/start` + `engine/pause` when the stale signature appears — same pattern as `range-firewall.timer`. | **Yes** — recurs on every engine reboot; today the fix is a manual POST plus a re-verify |
| B3 | `clean_engine_for_template` can SSH the cleanup into a foreign live engine on a shared mgmt IP | Resolve the target by VM identity (vmid via guest-agent) instead of a shared static IP; refuse when the IP is ambiguous. | **Yes if concurrent comps are normal** — they are: two other comps' VMs are up on these nodes right now, and the failure destroys a live engine's `.env`, event.conf and host keys |
| B4 | Fedora goldens cannot be built on SELinux-enforcing nodes | One line in the template build (`SELINUX=permissive` in `/etc/selinux/config`) — the doc already names this. | **Yes** — cheaper than the current "keep fedora off these nodes" |
| B5 | Freeze records commit+dirty but no gate reads it | **[DONE 2026-10-02]** `--freeze` refuses uncommitted deploy-path code and notes non-code dirt; `frozen_code_drift` warns at deploy when the frozen commit moved or code is uncommitted. The premise in this row's first draft was wrong: the drift gate keys on function hashes and is warn-only, so committing after a freeze never tripped it. | **Yes** — done |
| B6 | llama.cpp context ceiling is prose | Assert the local-blue `CTX` against the configured slot limit at launch and fail loudly. | **Yes** — currently a self-compaction trap you only find mid-run |
| B7 | PAM account-stage incident is unreproducible | The doc's own diagnostic kit is the fix: commit `tools/pam-bisect-lab.py` (plant rungs + `sshd2` + probe log) so the lab is one command. | **Conditional** — one occurrence to date; not worth hunting now, but the lab must exist before it recurs |
| B8 | Alpine template needs persistent `ssh_pwauth` + `%wheel` | Template-build lint that checks both. | **Maybe** — only if Alpine stays in the lineup |

### C. Not worth fixing — keep documented, with a link

- **Environment/hardware:** cyberfield .193 hard-downs (owner-confirmed hardware; the only fix is a
  power cycle), site-lab outage / ping-is-not-liveness, `ping` semantics.
- **Storage/design constraints:** thick LVM cannot snapshot (fallback exists), netplan 0600, VirtIO
  cold boot, Docker iptables re-sync (timer + re-assert already in place), datastore saturation and
  source-VM lock race (both obsoleted by linked clones + `-parallelism=1`).
- **Vendor-owned:** nakon's silent install failures (mitigated by the `nakon_failed_steps` tally +
  verify), winget/choco never exercised, the randomize→pin var gap (repo side is done via
  `REQUIRED_VARS`; upstream deferred).
- **Quotient feature gaps:** Windows scoring is port-open only; one session per account.
- **Process rules with working preflights:** challenge templates squatting vmids, concurrent
  golden/satellite vmid races (preflight refuses; the destroy ownership guard is the second line),
  stale ifupdown2 runtime state (node hygiene — worth one line in `range-healthcheck`, not a project).
- **The 38 fixed incidents:** keep the reasoning, but they are history. Move them to a dated archive
  or `reports/` and leave one-line pointers.

### D. Verdict on the four *labeled* open issues

| Labeled open issue | Verdict |
|---|---|
| cyberrange (.150) operational notes | **Rewrite, don't fix.** vmid 1000 is gone; hdd is 266 GiB, not ~900; the `TEZ_THIN_HEADROOM` update is correct. |
| llama.cpp context ceiling | **Standing constraint + cheap guard** (B6). |
| Noble dpkg-breakers | **Upstream; make it a list** (B1). We do not currently pin them, so patching the catalog scripts ourselves is not worth it. |
| Scoring round loop | **Fix properly** (B2). It is the only labeled open item that is ours, recurring, and cheap to automate. |

---

## 4. Proposed shape for the file

1. **Open traps** (target: under a screen). One line each, with `fix:` and `verified: <date>`. Nothing
   that is closed.
2. **Machine-readable broken-config list** in `constants.py` (B1), so this doc stops being the
   enforcement mechanism.
3. **Incident archive** — keep the 38 post-mortems (rationale included), moved to their own file or
   folded into the existing `reports/` they already link, leaving one-line pointers here.
4. **Node facts** split into the node/host docs (datastores, hardware, ifupdown2 hygiene).
5. **Security disclosures** into their own file, with the token-rotation status and the exact
   remaining exposure (remote blob) stated up front.

The text itself is accurate and well-cited; the problem is entirely one of genre-mixing and stale
"open" framing. Most of the 69 entries need no fix — they need to stop competing for attention with
the handful that do.
