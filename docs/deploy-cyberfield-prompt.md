# Full-dress deploy on the NEW CYBERFIELD range (10.0.0.193) — agent prompt

You are doing a full tezcatlipoca deploy of the extreme scrim-dress competition **from scratch on
the Cyberfield Proxmox node** — a SEPARATE cluster from the Cyber Realm (10.0.0.150/realm.hnasheralneam.dev,
node `proxmox`). The Realm is busy with another agent; do **not** touch it. Everything below targets
Cyberfield only. Work autonomously; never ask questions. Use `.venv/bin/python` for ALL driver
commands (system python3 lacks toml).

## Source-of-truth docs (read first)
- `/home/hna/dev/dawgsec/tezcatlipoca/` — this repo (create-competition.py, run-agent-scrim.py, .env)
- Cyberfield access + gotchas: `~/.hermes/infra-registry/servers/cyberfield.md` and the
  `devops:cyberfield-proxmox` skill (endpoint, token, node, template inventory, storage, untrustedbr)
- Competition to replicate: `competitions/scrim-extreme-2026-09-20/` (boxes.json, box_vulns.json,
  box_services.json, injects/, domain_roles.json — SAME content; only the template names and cluster
  wiring change)

## Why a separate range / what changes from the Realm run
tezcatlipoca is cluster-agnostic via `.env` (terraform reads `TF_VAR_*`; box templates resolve by
NAME from VMs tagged `template`; the engine resolves by `TF_VAR_template_vm_id`). To deploy on
Cyberfield you override connection + template/VMID wiring — you do NOT touch competition content.

## Cyberfield wiring (verified 2026-09-22) — apply to `.env`
```
TF_VAR_proxmox_endpoint=https://10.0.0.193:8006/
TF_VAR_proxmox_api_token=<secret — see ~/.hermes/infra-registry/servers/cyberfield.md>
TF_VAR_proxmox_node=pve
TF_VAR_datastore=hdrives-zfs            # cyberfield = its ZFS pool (realm used hdd; cyberfield is hdrives-zfs)
TF_VAR_template_vm_id=1007              # engine base = base-ubuntu24.04-fix on cyberfield (was 9106 on realm)
TF_VAR_vm_username=sysadmin
TF_VAR_ssh_public_key=<the same key the repo already carries in .env>
TF_VAR_ssh_private_key_path=../proxmox  # unchanged
```
**Box-template name map (cyberfield names differ from realm):** in `competitions/…/boxes.json`,
`box.template` must use the CYBERFIELD template names. Verified present on cyberfield:
```
base-windows-server    -> the Windows dc01/win01 base   (cyberfield vmid 1008)
base-ubuntu24.04-fix   -> the Ubuntu Linux base          (cyberfield vmid 1007)
base-debian13-lite-fix -> the Debian Linux base          (cyberfield vmid 1006)
```
Edit the new competition's `boxes.json` so `dc01`/`win01` ⇒ `base-windows-server`, `web01`/`db01` ⇒
`base-ubuntu24.04-fix`, `app01` ⇒ `base-debian13-lite-fix`. Re-run the nakon catalog check (below).

**VMID scheme / team identifiers:** team box VMIDs are `200 + identifier×10 + index`. Cyberfield's
template library occupies 1001–1011, 1101–1105, 1201–1211, plus engine vmid 1000. Default scrim
team identifiers are 101/102 → VMIDs 1210–1214 / 1220–1224, which **overlap cyberfield templates**
(1201–1211). Fix: in the new competition `teams.json`/authoring, set team identifiers to
**113 and 114** → team boxes land at 1330–1334 / 1340–1344 (free). Team subnets become
192.168.113.x / 192.168.114.x (engine creates per-team bridges vmbr113/vmbr114 automatically).

## Phase 0 — pre-flight gates (do not skip)
1. Cyberfield reachable: `curl -sk https://10.0.0.193:8006/` → 200. Node `pve`. Engine vmid 1000
   must be free (it is — confirm no template there before apply).
2. **Boot-verify every box template you rely on** (they were corrupted once and re-copied via ZFS
   send; verify, do not assume): for each of 1006/1007/1008, `POST .../clone` to a scratch VMID,
   start it, poll `POST /nodes/pve/qemu/<id>/agent/ping` → token arrives ~20s. Destroy the scratch.
   A template that never pings must be rebuilt from the Realm via the `qm remote-migrate` recipe in
   `servers/cyberfield.md` (realm clone → clean config → remote-migrate → rename → boot-test).
3. Engine template 1007 must boot WITH `qemu-guest-agent` and the ssh key for `sysadmin` (the engine
   cloud-init is NOT touched by terraform — `docs/usage-people.md` "Scoring engine template").
   Boot-test a scratch clone; confirm you can `ssh -i proxmox sysadmin@<engine-ip>` after boot.
4. Nakon catalog unchanged — vulndb stays at `10.0.0.121:3000` (see `vendor/nakon/.env`; do not move).
5. Author the cyberfield competition:
   `python create-competition.py` authoring path (or copy competitions/scrim-extreme-2026-09-20 →
   `competitions/scrim-dress-cyberfield-<date>`) with the box-template remap above and team
   identifiers 113/114. Run:
   `cd vendor/nakon && ../../.venv/bin/python -m nakon catalog check --boxes-json ../../competitions/scrim-dress-cyberfield-<date>/boxes.json --box-vulns ../../competitions/scrim-dress-cyberfield-<date>/box_vulns.json --box-services ../../competitions/scrim-dress-cyberfield-<date>/box_services.json`
   → **0 errors**.
6. Network note: box bridges are per-team (vmbr113/vmbr114), isolated, no uplink; the engine is the
   router. `untrustedbr` on cyberfield is a separate net (192.168.200.x, gateway VM 104) — irrelevant
   here. Blues/red later reach box IPs ONLY through the engine subnet, exactly like the Realm run.

## Phase 1 — deploy (fresh, all 7 phases)
`cd /home/hna/dev/dawgsec/tezcatlipoca && .venv/bin/python -u create-competition.py --competition scrim-dress-cyberfield-<date> --teams 2 --yes` in a **tracked background terminal**, full log to
`logs/deploy-cyberfield-<date>.log`. Expected 7 phases: terraform clone → engine bootstrap/docker
build → DNS/nakon plant (extreme pin set ~460 pins/team) → clone team2 → AD domain joins → seed
injects → 'is live'. Expect 40–80 min; Windows .NET 3.5 steps run 5–10 min quiet.

## Monitor loop
Every 4–5 min: `tail -25 logs/deploy-cyberfield-<date>.log` + `pgrep -f "create-competition.py"`.
Advancing log = healthy even if quiet. Any OTHER tezcatlipoca driver on THIS cluster (check
`pgrep`, and `/opt/nakon/.deploy-owner` on the engine) = stop and report a collision — do not fight.

## Failure playbook (handle, don't improvise beyond it)
- If a **Windows config FAILED with 'X is required' / empty-var**: remove that pin from the
  competition's box_vulns.json (realm-proven names already trimmed 534→459); revalidate catalog
  (Phase 0.5 command) → 0 errors; roll affected VMs to `tz-base` and resume the same phase.
- **Guest-agent timeout / 'agent is not responding'** on a fresh clone: it cold-boots slowly, wait
  90s and retry before declaring failure.
- Transient (qemu lock, apt 404, missing IPv4 on fresh clone): roll back to `tz-base` via
  `POST /nodes/pve/qemu/<VMID>/snapshot/tz-base/rollback` (cold-stops; poll status → start → poll
  `agent/ping`), then resume the same phase per its `[N/7]` marker.
- Resume form: `.venv/bin/python -u create-competition.py --competition scrim-dress-cyberfield-<date> --teams 2 --yes --from-phase N`
- Root-ssh/`qm` on the node only if you truly need a shell: cyberfield root SSH is refusale unless
  password-authed (`sshpass -p '<root password — see cyberfield.md>' ssh root@10.0.0.193`). Prefer the API token.
- On failure: NEVER `destroy-competition`; NEVER touch workshop-* / challenge-* templates, VM 104,
  VM 200, or anything outside the competition's own VMIDs. Max 2 repair-resume cycles; on the 3rd
  failure stop and report.

## When live (banner + exit 0)
1. `verify-competition.py competitions/scrim-dress-cyberfield-<date> --engine-ip IP --admin-password PW`
   (engine IP + admin pw read programmatically from `credentials.txt`; mask all passwords in reports).
2. Log evidence: team1 AND team2 each planted ~459 pins; domain joins done (`team113.local` /
   `team114.local`); injects seeded; tz-ready snapshots taken for all 10 boxes.
3. `grep -c FAILED logs/…log` — list any FAILED lines verbatim.
4. Scoreboard HTTP probe `curl -s -o /dev/null -w '%{http_code}' http://IP/` → 200.
5. Guest-agent spot-check one team1 Linux box + one team2 Linux box (web server active + a planted
   artifact, e.g. world-writable /var/www or a flag file). Leave everything RUNNING.

## Report
phases + durations · template boot-verify results · catalog check result · FAILED count+details ·
verify pass/fail per check · scoreboard code · spot-check · repairs made · any stragglers in the
competition VMID range not owned by the competition. No passwords.