# Security disclosures and secret hygiene

History of credential exposure in and around this repo, and what was done about each. Split out of
[known-issues.md](known-issues.md) on 2026-10-02: provenance belongs in its own file with an owner,
not mixed in with deploy incident lore.

## ⚠️ Open action — leaked Proxmox token is still live (verified 2026-10-02)

The token exposed by the 2026-10-01 `.env.pre-cde-20260929` leak **has not been rotated**. Verified
by read-only probe: the secret in that file is byte-identical to the one in the current `.env`
(same SHA-256), and it authenticates successfully against the cyberfield cluster
(`root@pam!agent`, `https://10.0.0.193:8006/api2/json/version` → HTTP 200). The file is untracked
now, but the same live secret is present in **five** files across four checkouts — every one of them
must be updated on rotation:

```
tezcatlipoca/.env
tezcatlipoca/.env.pre-cde-20260929
tezcatlipoca-testcomp/.env
tezcatlipoca-scale8/.env.pre-cde-20260929
tezcatlipoca-smoke/.env
```

Plus two local backups that contain the pre-rewrite history and therefore the blob:
`.tez-backups/all-refs-20261001-232202.bundle` and
`.tez-backups/pre-rewrite-20261002-002405.bundle` (both gitignored; kept as the history-rewrite
safety net).

**Required:** rotate that API token on the cyberfield cluster, then update all five files above.
Rotation is the only thing that closes the exposure — history rewrite cannot un-publish a blob
someone already cloned. Treat the old value as public forever. The local plaintext copies were
deliberately **not** scrubbed in the 2026-10-02 pass: their contents are identical to the live
`.env`, so redacting them would remove a usable backup without reducing exposure. After rotation they
are inert and can be deleted. The `.env` files are gitignored by the blanket `.env.*` rule (see
prevention below).

**Remaining exposure on the public remote:** `origin/main` still reaches the blob at `8cb755c`.
Local `main` was rewritten, but local vs remote have since diverged by 237/177 commits, so a
force-push is no longer the cheap fix it was on 2026-10-01 — rotation is. If the remote is
force-pushed later, coordinate it; `refs/remotes/origin/*` is deliberately left un-rewritten so
`git status` keeps showing the divergence.

## Student portal secrets (where they live)

A competition with Compfile `portal 1` carries three more secrets ([portal.md](portal.md)):

| Secret | Where it lives |
|---|---|
| Per-comp console token (`tezcon-<comp>-<run_id>@pve!portal`, `VM.Console` on this comp's team VMs only) | `.deploy_state.json` (`portal_console_tokens`), `competitions/<id>/portal.json`, and `/opt/tez-portal/portal.json` on the engine |
| Portal session-signing key | `.deploy_state.json` and `/opt/tez-portal/.env` |
| Cloudflare tunnel token | Event env file (`TEZ_PORTAL_TUNNEL_TOKEN`) and `/opt/tez-portal/.env` |

File modes:
- Comp-dir files are 0600 and gitignored. `portal-access.log` is also gitignored, because it holds
  team logins and source IPs.
- Engine files are 0600 root.

Teardown deletes the console user on every node, by exact name. If a destroy warns that it could
not, re-run it; until then the token can still open consoles on that comp's VMs.

## Security disclosure history

- **2026-10-01 env-variant leak**: `.env.pre-cde-20260929` was tracked from `8cb755c`
  (the 2026-09-30 loadtest squash-merge) and reached the public remote. It carried a live
  Proxmox API token (`TF_VAR_proxmox_api_token`, `root@pam!agent=…`), the real endpoint,
  `TF_VAR_box_password`, and per-team passwords. It slipped through because `.gitignore`
  listed `".env"` and `".env.realm-backup*"` **by name** and missed the hand-named variant.
  Two independent fixes, both required:
  1. *Prevention* — `.gitignore` now carries a blanket `.env.*` with `!.env.example`, and
     `tests/test_secret_hygiene.py` fails the suite if any `.env` variant is tracked, if a
     tracked file matches a secret shape, or if code reads an env var neither declared in
     `.env.example` nor allowlisted in the test. Rule-by-rule gitignore edits silently reopen
     this class, which is why the guard is a test and not a comment.
  2. *Removal* — history rewrite via `tools/purge-path-from-history.sh`, the rehearsed
     procedure: `filter-branch --index-filter`, then **delete `refs/original/*`, expire
     reflogs, `gc --prune=now`**. That last step is the one that matters and the one usually
     skipped — after the filter-branch alone the file is gone from every tree while the blob
     is still in the object store and still cloneable.
  **The token is permanently public until rotated**; removing it from history does not
  un-leak it. Recorded here rather than quietly closed, because the shape (a hand-named
  secrets file outside the ignore pattern) will recur.
  Scope: `main` and local-only `scale8-2026-10-01` were rewritten; `testcomp-cyberfield-2026-09-29`
  and tag `v0.1.0` never contained it. `refs/remotes/origin/*` is deliberately left
  un-rewritten so `git status` keeps showing the divergence — the public remote retains the
  blob until someone force-pushes.

- **2026-08-06 git-history audit**: found a real admin password and team passwords for the CDE
  2026 competition (since torn down) in a pre-2026-07-09 commit predating the
  "stop tracking competition secrets" history rewrite. The commit is no longer reachable from
  any branch but was pushed to GitHub before the rewrite — treat it as permanently public. The
  exposed passwords were rotated and are unused. No real Proxmox API token or nakon/vulndb
  password was found anywhere in this repo's history. A separate `nakon` checkout must audit its
  own history independently.

- **2026-09-29 tracked terraform artifacts audit**: 21 files tracked on the PUBLIC GitHub remote
  carried real per-competition credentials — 9 `terraform.tfvars.json` (`box_password`,
  `teams.teamN.password`), 9 tfstate files (`team_passwords` outputs), and three
  `competitions/m4-validation-2026-09-25/` run logs (`m4-resume-current.log` with three
  `.deploy_state.json` values, plus `m4-scenario8-team-rebuild.log` and `m4-verify-current.log`
  with one each). No standing infrastructure secrets were exposed (no Proxmox API token or current
  `.env` value appears anywhere in the tracked tree), and every affected competition was already
  torn down, so the passwords were operationally dead — nothing to rotate. Resolved by
  curation: the three best-documented competitions keep their terraform artifacts tracked as
  reference (shakedown-5x4-2026-09-28, pfsense-ad-2026-09-27, scrim-extreme-cyberfield-2026-09-22);
  everything else is untracked, and the gitignore now covers the layouts that slipped through
  (comp-dir top-level `*.tfstate*`/`terraform.tfvars*` — exactly how
  `scrim-dress-2026-09-20/terraform.tfstate` got committed — plus `competitions/*/*.log` and
  root `terraform/terraform.tfvars*`). History keeps the removed passwords (same policy as the
  2026-08-06 audit: treat as permanently public; they are dead). Separately, the
  same-type-2box closeout's terraform commit was amended out pre-push — its content never
  reached the remote; 0787d42's rules predate this wider gap closure.
