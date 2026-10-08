# Transfer prompt — scrim-one rehearsal on Cyber Realm (.150)

Copy everything below the line into a fresh session.

---

You are taking over a scrim rehearsal in progress. Work autonomously; do not ask questions unless
you hit a genuine GO/NO-GO failure. **Read the incident section before touching anything on the
Proxmox node.**

## Ground truth

- Repo: `/home/hna/dev/dawgsec/tezcatlipoca` — branch `main` @ `2860958` (`v0.2.0-3-g2860958`),
  `vendor/nakon` submodule @ `22360ba` (the 0-step fix v0.2.0 requires). Use
  `.venv/bin/python` (3.14) for every driver command; `terraform` is system-wide (1.16.5).
- bad-auto: `/home/hna/dev/dawgsec/bad-auto` — `main` @ `d110d69` (ff'd, clean; uncommitted
  realm-imix WIP was stashed as `stash@{0}` if ever needed).
- Proxmox target: `realm.hnasheralneam.dev` / `10.0.0.150`, node **`proxmox`**, datastore **`hdd`**.
  Root SSH key: `~/dev/dawgsec/tezcatlipoca/proxmox` (`ssh -i ... root@10.0.0.150`). API token lives
  in the repo `.env` (never print its value).
- `.env` (repo root) is already set for this node: endpoint realm / node `proxmox` / datastore `hdd`
  / `TF_VAR_template_vm_id=955` / `TF_VAR_team_identifiers=120` /
  `TF_VAR_engine_mgmt_ip=10.0.0.252` / `TF_VAR_engine_mgmt_gw=10.0.0.1`. The 9088 ghost vmid trap is
  fixed; do not reintroduce it.
- Secrets: `competitions/scrim-one/.deploy_state.json` (gitignored, run id `run-b6c2db3e`) holds the
  engine/team/box/inject credentials. **Never print its values**, never commit it.
- Never enumerate or describe the planted misconfigurations in chat (they live in
  `box_vulns.json`); reference counts/boxes only. The owner plays blue in person later.

## Deployed range (deploy phases 1–8 COMPLETE — do not redeploy)

- Engine: VMID **1000** `quotient-engine` @ **10.0.0.252**, Quotient on `http://10.0.0.252/`
  (round loop active), SSH `sysadmin@10.0.0.252`.
- Engine template **1140**; golden templates **1150–1155** (dc01, win02, web01, mail01, db01,
  splunk01). **Keep them** — the owner plays these boxes in person.
- Team boxes **1400–1405** on bridge `vmbr120`, subnet `192.168.120.0/24` (team id 120).
- All plants verified with 0 failures; AD promoted; 12 injects created; competition seeded and the
  engine unpaused. Inject login is in the state file.
- Deploy log: `logs/deploy-scrim-one-2026-10-06.log`. Plan: `competitions/scrim-one/`.

## Work in progress: pfSense in-path (fw01)

Already done: `competitions/scrim-one/boxes.json` now declares an `fw01` box
(`pfsense-provision`, `unmanaged: true`, `in_path: true`), the Compfile carries
`firewall_dnat 4470->10.0.0.244`, the per-team config is rendered at
`competitions/scrim-one/pfsense/config-team120.xml`, and the transit bridge **`vmbrW120`** exists on
the node (persistent in `/etc/network/interfaces`).

Remaining (v0.2.0 automates all of this in phase 5 when the lineup declares an `in_path` box —
prefer driving the `firewall_ops` functions manually over resuming phases, because a
`--from-phase 5` resume re-runs the non-idempotent plant/domain passes):

1. Full-clone template **957 `pfsense-provision` → 1410**; `net0` → `vmbrW120`, `net1` → `vmbr120`,
   onboot, start.
2. Engine needs a transit NIC: add one on `vmbrW120` (**cold stop+start required** for the PCI
   device to appear — `qm stop 1000`, `qm set 1000 --net2 virtio,bridge=vmbrW120`, `qm start 1000`).
3. Push `config-team120.xml` to the firewall over SSH (engine borrows `192.168.1.2/24` on its team
   NIC to reach the provisioning template's `192.168.1.1`; `firewall_ops.bootstrap_firewalls`
   implements this, including the retry-while-booting logic).
4. Engine cutover: rewrite `/etc/netplan/60-team-ifaces.yaml` so the team NIC carries no address and
   the transit NIC holds `172.31.120.1/30` + route `192.168.120.0/24 via 172.31.120.2`
   (`firewall_ops.cut_over_engine`).
5. Verify in-path (`firewall_ops.verify_in_path`): engine routes the team subnet via
   `172.31.120.2`, firewall WAN answers, first managed box reachable **through** the firewall.
6. Re-run the service-fix pass afterwards (`fix_services_on_boxes`) and re-verify.

## Next gates after the firewall

1. `./.venv/bin/python verify-competition.py competitions/scrim-one --strict-services` — every gate
   must PASS (`--fix-round-loop` if the round loop stops after any engine restart).
2. bad-auto coverage: deploy red01 (vmid **999**, storage **`hdd`** — `hdrives-zfs` does not exist on
   this node, IP **10.0.0.244**, gw 10.0.0.1) and prove every scored pin flips DOWN and back UP.
3. 3-hour agent scrim, run in a tracked background terminal, **with `--keep-range`**:
   `run-agent-scrim.py --competition scrim-one --teams 1 --duration-min 180 --blue-watchdog --keep-range`
   Endpoint roster (owner's instruction: the weaker model goes to blue):
   - blue (team1) = `http://100.64.0.19:8083/v1` → `qwen3.6-35b-mtp`
   - red = `http://100.64.0.19:8888/v1` → `Qwen3.8-Flash-Next` (alt: local
     `http://10.0.0.143:8080/v1` → `qwen3.8-27b`, ~60k prompt cap)
   Probe every endpoint before rostering; the local/remote GPU hosts host multiple servers that
   serialize — confirm capacity first. `OPENROUTER_API_KEY` is the fallback if endpoints are down.
4. Do **not** tear down the range unless asked; goldens and team boxes stay for the owner.

## INCIDENT — read before touching the node's networking

2026-10-07: an `ifreload` / networking restart issued on this live node re-initialized every bridge
and orphaned the host-side legs of **all** running VMs (plain taps *and* firewall veth legs). The
node never went down and no VM was stopped, but the whole realm appeared down until the legs were
re-enrolled. Full write-up: `docs/reports/2026-10-07-realm-networking-incident-and-recovery.md`.

Hard rules:
- **Never** run `systemctl restart networking` (or any full reload) on `10.0.0.150`.
- Create bridges with `pvesh create /nodes/proxmox/network --iface vmbrX --type bridge --autostart 1`
  (flag is `--comments`, not `--comment`).
- If legs are ever orphaned again: re-enrol `tap<vmid>i<nic>` and `fwpr<vmid>p<nic>` to their
  configured bridges, mapping by **interface name** (tap MACs are host-random); `qm list running`
  is not a valid subcommand.
- `qm guest ping` does not exist in this PVE version; test QGA with `qm guest exec <vmid> -- true`.
- Do not reboot the physical host. The owner is not home.

## Estate caveats from the recovery

- Service VMs are DHCP — their IPs drift (VM116 .197, VM117 .181, VM136 .16, VM137 .74, VM921 .246,
  VM111 .102, VM115 .121, VM107 .33). Do not assume the VM-number address.
- **matrix (VM136)**: the Cloudflare tunnel dials `10.0.0.14`, which is the VM's *configured* static
  but not what DHCP gave it (`.16`). `.14` was added as a runtime secondary so matrix.dawgsec.com
  works now — **not reboot-persistent**; make it durable when convenient.
- All public URLs verified 200/302 at handoff: guacamole, nextcloud, vms.dawgsec.com, matrix,
  timestat.dawgsec.com.
- **LXC containers** share the firewall-leg pattern: VMID **200 `timestat`** (bridge vmbr0,
  DHCP → `10.0.0.111`, service = gunicorn on `:8000`) had `fwpr200p0` orphaned and was fixed the
  same way. Any future leg sweep must cover `pct list` as well as `qm list`. Only LXC 200 exists.
- Final state at handoff: **zero orphaned taps or firewall legs** (VMs + LXC) and **zero tunnel
  origin failures** in the preceding 8 minutes.
