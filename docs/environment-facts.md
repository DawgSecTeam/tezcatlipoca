# Environment facts — hosts, templates, vmids

Pipeline/host behavior that holds on any node. **Estate-specific ground truth — which
hosts exist, their endpoints and node names, datastores, template vmids, token locations,
and what is live right now — lives in `infrastructure-status.md` at the repo root
(gitignored, local only).** Read that file before touching a host; it wins over anything
here or in tracked docs. Re-verify anything vmid/space-related before relying on it; it
moves.

## Hosts

The estate has more than one Proxmox host, reached with different endpoints, API tokens,
and datastores; they are not interchangeable, and at least one is routinely shared with
non-range VMs. Endpoint/token/datastore per host: `infrastructure-status.md`. The central
dashboard that range runs are submitted to is also recorded there.

Only the Proxmox API port (:8006) and the guest-agent channel are reachable from the dev
host; other VM ports TCP-RST. **Ping is not a liveness signal** (a tailnet bridge can
answer ICMP while forwarding nothing) — probe `curl -k https://<node>:8006/` for any HTTP
code, not `000`. State, snapshots, and disks survive a node outage; resume with
`--from-phase`.

## Templates

Cloud-init capability is what makes a template usable. Which template vmid carries which
base image on which host: `infrastructure-status.md`.

- **`base-fedora44` ships without cloud-init.** Use the `-fix` variant where one exists.
  Fedora also needs SSH password-auth forced via a `00-*` drop-in, `named` bound beyond
  `127.0.0.1`, and `httpd` with `ServerName localhost` — folded into
  `fix_services_on_boxes`.
- **Alpine needs persistent `ssh_pwauth` and a `%wheel` rule in the main sudoers file.**
  The image ships password auth off and no `%wheel` rule, so a NOPASSWD entry living only
  in `sudoers.d` vanishes if a `writable-sudoers` plant makes that dir 0777.
- **Fedora + the guest agent is confined** (SELinux `virt_qemu_ga_t`): `dnf`/`rpm`/
  `systemctl` denied, `setenforce 0` denied. SSH as the cloud user lands `unconfined_t`
  with real root — the working path. Keep Fedora out of agent-only lineups, or set
  `/etc/selinux/config` permissive at template-build time.
- **Windows** ships all firewall profiles disabled, so firewall-rule effects do nothing
  until `Set-NetFirewallProfile -All -Enabled True` (`bootstrap_windows_box` does this,
  plus enabling `RemoteDesktop-UserMode-In-TCP/UDP`, since the DC template ships that rule
  group disabled too). A Windows clone's guest agent takes ~8 min to appear.

## vmid occupancy

- **Default team identifiers (101+) collide on shared hosts.** Pass
  `TF_VAR_team_identifiers` explicitly on any host that is not empty; the occupied blocks
  per host are in `infrastructure-status.md`.
- **red01's slot is per-competition, never 999** (see [AGENTS.md § bad-auto
  isolation](../AGENTS.md)): vmid from Compfile `red01_vmid`, else `<scoring-vmid>+170`;
  IP from Compfile `red01_ip`, else `10.0.0.198`. Both are preflight-gated — an occupied
  vmid or an answering IP is a refusal naming the owner; an unverifiable network refuses
  the default too. Concurrent comps need scoring vmids spaced more than 10 apart (the
  +140/+150/+170 offsets and golden box count must not overlap).
- **Engine management IPs are shared across concurrent deploys unless separated.**
  `clean_engine_for_template` SSHes to the *planned* mgmt IP, so a collision can land
  cleanup on a live foreign engine's `.env`/`event.conf`/host keys. Give each concurrent
  comp its own `TF_VAR_engine_mgmt_ip`; the preflight scans cluster-wide and refuses the
  default address when any guest on it is unverifiable.
- **Reclaim orphaned ranges with `destroy-competition.py` from the matching comp dir**,
  never an ad-hoc sweep. Occupancy is not tracked in docs — check the Proxmox API on the
  target host before a deploy.

## Headscale

External headscale instance the pipeline administers over ssh+sudo (`TEZ_HEADSCALE_*` in
the deploy env; host and credentials: `infrastructure-status.md`;
[remote-access.md](remote-access.md)). Version/behavior notes below are host-agnostic.

- Runtime `headscale/headscale` (v0.29.x) in a container with an sqlite DB; `docker` is
  root-only, so every CLI call is `sudo docker exec headscale headscale …`. Embedded DERP
  enabled for clients with no direct path.
- **DNS**: MagicDNS on, base domain `vpn.hnasheralneam.dev`
  (`eng-<comp>.vpn.hnasheralneam.dev`).
- **Policy**: `policy.mode: file`; the pipeline regenerates the whole policy from
  `tezcatlipoca-remote-access.json` beside it. Pre-existing nodes are untagged and keep
  access via `group:full-access`.
- **v0.29 CLI**: no `routes` command (approvals ride the policy's `autoApprovers`; list
  with `nodes list-routes`); policy v2 group/grant sources need `@`-suffixed usernames (a
  bare name parses as a host reference); `policy check` wants `--file`;
  `users destroy -n <name> --force` refuses while the user owns nodes.
- **Tag-sourced grants do not reach routers' packet filters** — a tag-sourced dial
  black-holes at the router even though the node's own routes are filtered correctly;
  user/group sources work. tezcatlipoca keys participant grants on usernames, not tags
  ([remote-access.md](remote-access.md)). Upstream-worthy defect.
- The VPS is itself a tailnet node and runs the mgmt-LAN subnet router. Probe service
  ports, never ping, for the same ICMP-without-forwarding trap as above.

## Node runtime behavior

- **A shared host's RAM is the binding constraint for satellite-heavy Windows ranges.**
  Check the live margin in `infrastructure-status.md` (or the API) before placing several
  Windows-heavy teams. Deploy time budget: [multi-node.md § Phase
  budget](multi-node.md#phase-budget-8-teams--5-boxes-two-nodes).
- **The management network is one flat L2 across hosts.** `TF_VAR_engine_mgmt_ip`/
  `jump_mgmt_ip` are contested cluster-wide, not per host — the default `10.0.0.250` is
  whatever every competition gets unless set otherwise. **Always set an explicit, checked
  `TF_VAR_engine_mgmt_ip`** when anything else is running on the estate; the preflight
  scans cluster-wide and refuses the default when it's unverifiable.
- **netplan refuses world-readable configs** — the engine's team-NIC netplan files are
  mode `0600`.
- **Docker re-syncs iptables on every container start/restart** (`FORWARD` → DROP, team
  rules wiped). `range-firewall.timer`/`range-healthcheck.timer` and
  `ensure_nat_forwarding` cover this; see
  [architecture.md](architecture.md#network-and-isolation).
- **Quotient crash-loops without `event.conf`**, so it's pushed before nakon runs.
- **New VirtIO NICs need a cold boot** — a guest-level reboot doesn't trigger the PCI scan.
- **Fresh clones can boot with an empty `/etc/resolv.conf`** (cloud-init ignores
  `dns.servers` on a static IP), so `fix_dns_on_boxes` runs in the deploy. Never point
  boxes at their own team's `dns*` box from Terraform — deadlock (its bind9 needs a
  working resolver to install via apt).
- **Stale ifupdown2 state breaks `ifreload -a` node-wide**: `/run/network/ifstatenew` can
  carry dead bridges from gone comps, failing every API-driven network change on a missing
  `/sys/class/net/vmbrNNN/brif/` (invisible via the API). Repair:
  `rm /run/network/ifstatenew && ifreload -a`. Tear team bridges down with `ifdown`, never
  raw `ip link del` (which creates this state).
- **Quotient secrets are plaintext-but-0600** on the engine (`/opt/quotient/.env`),
  generated fresh per deploy. Box/credlist usernames are themeable and not secret;
  passwords always are.
