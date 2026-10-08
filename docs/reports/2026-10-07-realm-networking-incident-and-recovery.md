# Incident & recovery — Cyber Realm node networking (2026-10-07)

## Summary
While wiring the pfSense in-path firewall for the `scrim-one` rehearsal, an `ifreload`/networking
restart was issued on the **live** Proxmox node `10.0.0.150` (`realm.hnasheralneam.dev`, node
`proxmox`). That re-initialized every Linux bridge on the node and orphaned the host-side network
attachment of every running VM. The node itself never went down (uptime unbroken, 6 days) and no
VM was stopped or destroyed — but the whole estate (guacamole, nextcloud, matrix, dex, ldap,
vulndb, fluffychat, the Cloudflare tunnel, the workshop portal, and the scrim range) lost LAN
connectivity simultaneously, presenting as "the realm is down".

## Root cause
`systemctl restart networking` / `ifreload -a` destroys and re-creates each bridge. Anything
dynamically enslaved by QEMU/PVE (`tap<vmid>i<nic>`) or by the PVE firewall plumbing
(`fwpr<vmid>p<nic>` ↔ `fwvp<vmid>p<nic>`) is not re-added by the reload, so all VM legs were left
with no bridge master.

## Recovery (no VM or host reboot required for most of it)
1. Re-enrol plain taps to their configured bridges, mapping by **tap name**, not MAC (tap MACs are
   host-random; `qm list running` is not a valid subcommand — parse `qm list`):
   `ip link set dev tap<vmid>i<nic> master <bridge>`
2. Re-enrol firewall legs (VMs with `firewall=1` on a NIC):
   `ip link set dev fwpr<vmid>p<nic> master <bridge>` and, where present,
   `ip link set dev fwvp<vmid>p<nic> master fwbr<vmid>i<nic>`.
   A full sweep of every running VM's `firewall=1` NICs found **28** orphaned legs (7 known + 21
   more, including both NICs of guacamole VM111 and the workshop VMs).
3. Container (LXC) legs have the same pattern and were missed by the initial QEMU-only sweep:
   VMID **200 `timestat`** (`net0 bridge=vmbr0 firewall=1 ip=dhcp`) had its `fwpr200p0` leg
   orphaned; re-enrolled with `ip link set dev fwpr200p0 master vmbr0`. Sweep **both** `qm list`
   and `pct list` — only LXC 200 exists on this node today.
4. Guests that were unresponsive on the network and had silent QGA were cold-restarted
   (`qm stop` + `qm start`) — VM 115, 116, 117, 107. Their consoles showed clean boots; QGA was
   actually alive (`qm guest ping` does not exist in this PVE version — my earlier "agent NO"
   readings came from an invalid command; use `qm guest exec <vmid> -- true`).
4. Service IPs had drifted (DHCP): e.g. VM116 = 10.0.0.197, VM117 = 10.0.0.181, VM136 = 10.0.0.16,
   VM137 = 10.0.0.74, VM921 = 10.0.0.246, VM111 = 10.0.0.102. Ping tests against the *VM-number*
   addresses were therefore misleading.

## Post-recovery verification (all green)
- ldap 10.0.0.197:389 open; vulndb 10.0.0.121:3000 200; dex 10.0.0.181:5554 200;
  matrix 10.0.0.14:8008 200; fluffychat 10.0.0.74:80 308; nextcloud-aio :8080 open (all AIO
  containers healthy); guacamole/portal 10.0.0.102:5000+8080 open.
- Public: guacamole 200, nextcloud 200, vms.dawgsec.com 200, matrix 200.
- Scrim range: engine 10.0.0.252 up, VMIDs 1000 + 1400–1405 running, goldens 1150–1155 present,
  Quotient round loop alive.

## Known follow-up
- **matrix (VM136)** runs on DHCP `10.0.0.16` while its `/etc/network/interfaces` declares static
  `10.0.0.14`, which is the address the Cloudflare tunnel dials. `.14` was added at runtime as a
  secondary address (`ip addr add 10.0.0.14/24 dev eth0`) — this is **not reboot-persistent**.
  Durable fix: make the guest actually use its static config (or update the tunnel origin).
- The `vms.dawgsec.com` portal (10.0.0.102:5000) and the tunnel recovered once the guac VM's legs
  were re-enrolled.

## Hard rule for the future
Never run `systemctl restart networking` (or any full networking reload) on the live realm node.
Create bridges with `pvesh create /nodes/<node>/network --iface vmbrX --type bridge --autostart 1`
(`--comments`, not `--comment`), then `ifreload -a` only if strictly necessary. If a full reload
ever happens again, re-enrol taps/fwpr legs as above and verify per-VM.
