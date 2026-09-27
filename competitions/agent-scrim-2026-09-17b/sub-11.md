# DNS and Name-Resolution Integrity Check

## Findings and actions

| Host | Evidence | Action |
|---|---|---|
| app01 (192.168.102.5) | `bind9` is active, `named-checkconf` passes, and BIND listens on TCP/UDP 53 on the team address and localhost. Configuration contains only the standard root/localhost/reverse zones; no `meridian.local` or `portal.meridian.local` zone is present. | Left the healthy scored DNS daemon unchanged. Recorded the missing authoritative zone as a design gap rather than fabricating ward records. |
| web01 (192.168.102.4) | `/etc/hosts` has no active `meridian.local` or `198.51.100.77` entries. | No change. |
| app01 (192.168.102.5) | `/etc/hosts` has no active `meridian.local` or `198.51.100.77` entries. | No change. |
| db01 (192.168.102.6) | `/etc/hosts` has no active `meridian.local` or `198.51.100.77` entries. | No change. |
| dc01 (192.168.102.2) | Windows hosts file has no active redirect entry. The file contains only the standard commented template plus inactive `Pinecrest portal fast-route` comments. | No change. |
| win01 (192.168.102.3) | Windows hosts file has no active redirect entry. The file contains only the standard commented template plus inactive `Pinecrest portal fast-route` comments. | No change. |

## Domain-resolution verification

The Linux host-file sweep and both Windows host-file sweeps found no active hijack of Meridian names or redirect to `198.51.100.77`. app01's BIND process remains active, validates successfully, and is reachable on the team DNS address. The `meridian.local` authoritative zone is absent, so ward records cannot be verified as authoritative from app01; no guessed records were added during the scoring window. The scored HTTP, DNS, SQL, WinRM, SMB, and RDP services remained available at the latest scoreboard check.

## Residual risk

The internal `meridian.local` authoritative zone and records need reconciliation with the intended domain DNS design after the event. Deploy the approved zone and records through change control, then test domain authentication and application dependencies before enabling it broadly.
