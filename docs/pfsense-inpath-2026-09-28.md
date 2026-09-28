# pfSense in-path firewall — runbook & postmortem (2026-09-28)

Competition `pfsense-ad-2026-09-27`, 4 teams, on **cyberrange** (10.0.0.150, node `proxmox`).
Goal: an in-path pfSense firewall (pure router) in front of every team, routing all scoring
traffic, left clean/undamaged (infrastructure, not a scored target).

## Result

All 4 teams route **engine → transit → pfSense → team boxes**; the engine holds no address on
the team bridges, so pfSense is the sole path. Verified: WinRM/LDAP (DC), nginx (web01),
apache+bind (app01) all reachable *through* each team's pfSense. Reboot-persistent on both sides.

## Topology (per team `<id>`, pure-router / no outbound NAT)

```
engine ─transit<id> (172.31.<id>.1/30)─ vmbrW<id> ─ pfSense WAN vtnet0 (172.31.<id>.2/30)
                                                     pfSense LAN vtnet1 (192.168.<id>.1/24) ─ vmbr<id> ─ boxes
```
- Engine: transit NIC `172.31.<id>.1/30`, route `192.168.<id>.0/24 via 172.31.<id>.2`, **no**
  `192.168.<id>.1` (pfSense owns it). Keeps its `MASQUERADE 192.168.0.0/16 → internet`, so
  box→internet goes box → pfSense → engine → NAT.
- pfSense: WAN `172.31.<id>.2/30` (gw = engine), LAN `192.168.<id>.1/24` (the boxes' gateway),
  **outbound NAT disabled** (engine NATs; keeps engine→box scoring un-NAT'd), a WAN pass rule for
  the routed scoring, SSH on.
- Boxes: unchanged — gateway `192.168.<id>.1`, now the pfSense LAN.

## What did NOT work (and why) — don't repeat these

1. **Host-side ZFS config injection breaks pfSense boot.** The template is pfSense 2.7.2 on
   ZFS (pool name `pfSense`). Importing the clone's pool on the Proxmox host to write
   `/cf/conf/config.xml` makes it unmountable by pfSense's FreeBSD loader — `Mounting from
   zfs:pfSense/ROOT/default failed with error 22` — because the host runs **OpenZFS 2.4.4**,
   newer than pfSense's loader. Proven: a plain clone boots fine; an import/export-cycled clone
   does not. (Isolating the clone's device during import and `zpool reguid` cleanly avoids the
   *pool-GUID collision* that suspended a pool earlier, but the version gap still kills boot.)
   **Also: never rename the pool** — pfSense hardcodes the name `pfSense`.
2. **`fetch` over WAN fails** — pfSense reverts a manual `ifconfig` on the WAN (managed) iface.
3. **IDE CD-ROM isn't seen** by FreeBSD on hot-attach, and even after a reboot `mount_cd9660
   /dev/cd0` gave `Invalid argument`.
4. **Engine NIC hotplug is unreliable past the first NIC** (`net6+` didn't appear in the guest,
   even after a PCI rescan). Use one engine reboot with all transit NICs instead.

## The non-jank method (reproducible)

Per team, no host ZFS, no WebGUI:
1. Host: create transit bridge `vmbrW<id>`; `qm clone <pfsense-template> <pf-vmid> --full`;
   `qm set <pf-vmid> --net0 virtio,bridge=vmbrW<id> --net1 virtio,bridge=vmbr<id>`; start.
2. Serve the per-team `config-team<id>.xml` over HTTP from the engine (reachable at the engine's
   team-bridge address `192.168.<id>.1:PORT`).
3. Drive the pfSense **console** (`qm sendkey` + `qm monitor … screendump` to read it): option 8
   (shell), then — the key trick — put a temp IP on the **unassigned LAN NIC** (pfSense does NOT
   revert `vtnet1`): `ifconfig vtnet1 inet 192.168.<id>.250/24 up`, then
   `fetch -o /cf/conf/config.xml http://192.168.<id>.1:PORT/config-team<id>.xml`, then `reboot`.
   Wait until the VM is truly at the menu before sending keys, or keystrokes interleave with boot.
4. Engine cutover, all teams at once & persistently: add transit NICs to the engine VM
   (`net5..net8`), rewrite the netplan (`/etc/netplan/60-team-ifaces.yaml`) to **MAC-match** each
   transit NIC (`set-name: transit<id>`, `172.31.<id>.1/30`, route `192.168.<id>.0/24 via
   172.31.<id>.2`) and drop the `192.168.<id>.1` on the team NICs; reboot the engine once.
5. `qm set <pf-vmid> --onboot 1` on each firewall so it survives a host reboot.

## Real bug fixed (committed)

`gen_pfsense_config.py` generated the WAN pass rule with
`<destination><network>192.168.<id>.0/24</network></destination>`. pfSense's `<network>` field
takes a keyword (`lan`/`wan`/an alias), **not a raw CIDR**, so it silently dropped the rule and
the firewall's default-deny blocked engine→box scoring (forward path timed out). Fix:
`<network>lan</network>` (pfSense expands to the LAN subnet). After the fix the rule loads as
`pass in quick on vtnet0 … from any to <LAN__NETWORK>` and every box scores through pfSense.

## Verification

- From the engine: WinRM/LDAP/http/dns to each box succeed, and the only route to
  `192.168.<id>.0/24` is `via 172.31.<id>.2` (pfSense) — so traffic is genuinely in-path.
- `pfctl -sr` on each pfSense shows the WAN pass rule loaded.
- Reboot-persistent: engine netplan brings the transit links up on boot; firewalls are
  `onboot=1` and hold their config on disk.

## Follow-ups (toolchain)

- Fold this into the pipeline as the `unmanaged` box path: a declarative `inject_pfsense`
  step (clone + console-fetch bootstrap) + terraform transit bridges / engine transit NICs +
  netplan, so it is not a manual console dance.
- The pfSense template predates cloud-init-style provisioning; a future template with a serial
  console + SSH-on by default would remove most of the console driving.
