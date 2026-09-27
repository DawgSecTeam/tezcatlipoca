# DNS and Name-Resolution Integrity Check

## Findings and actions

| Host | Evidence | Action |
|---|---|---|
| app01 (192.168.102.5) | `bind9` is active and `named-checkconf` passes. `named` listens on TCP and UDP 53 on the team address and localhost. Searches of `/etc/bind` found no `meridian.local` zone or records; queries for `meridian.local` and `portal.meridian.local` returned no answer. | Left the healthy DNS daemon unchanged. Recorded the missing authoritative zone as a design gap rather than fabricating ward records. |
| web01 (192.168.102.4) | `/etc/hosts` contains no `meridian.local` or `198.51.100.77` entries. | No change. |
| app01 (192.168.102.5) | `/etc/hosts` contains no `meridian.local` or `198.51.100.77` entries. | No change. |
| db01 (192.168.102.6) | `/etc/hosts` contains no `meridian.local` or `198.51.100.77` entries. | No change. |
| dc01 (192.168.102.2) | Windows hosts file contains no `meridian.local` entry. | No change. |
| win01 (192.168.102.3) | Windows hosts file contains no `meridian.local` entry. | No change. |

## Domain-resolution verification

The Windows hosts-file sweep was clean on both domain-joined systems. The Linux sweep was clean on all three Linux hosts. app01's BIND process remains active and its configuration validates successfully. The authoritative `meridian.local` zone is absent, so Meridian ward names cannot be verified as authoritative from app01; no guessed records were added during the scoring window. The scored HTTP, DNS, SQL, WinRM, SMB, and RDP services remained available.

## Residual risk

The internal `meridian.local` authoritative zone and records need reconciliation with the domain DNS design after the event. Deploy the approved zone and records through change control, then test domain authentication and application dependencies before enabling it broadly.
