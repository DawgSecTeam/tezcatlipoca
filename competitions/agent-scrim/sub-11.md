# DNS and Name-Resolution Integrity Check

## Findings and actions

| Host | Evidence | Action |
|---|---|---|
| app01 (192.168.101.5) | `named`/`bind9` active; TCP and UDP 53 listening. `named-checkconf` passed. The only configured non-automatic zone is the default `localhost` zone; `meridian.local` and `portal.meridian.local` returned NXDOMAIN. No duplicate/stale Meridian zones were present. | Left the working DNS daemon unchanged. Recorded the missing `meridian.local` zone as a configuration gap rather than fabricating ward records. |
| web01 (192.168.101.4) | `/etc/hosts` contained no `meridian.local` or `198.51.100.77` entries. Resolver uses the local systemd-resolved stub and `team101.local` search domain. | No change. |
| app01 (192.168.101.5) | `/etc/hosts` contained no `meridian.local` or `198.51.100.77` entries. | No change. |
| db01 (192.168.101.6) | `/etc/hosts` contained no `meridian.local` or `198.51.100.77` entries. Resolver uses the local systemd-resolved stub and `team101.local` search domain. | No change. |
| dc01 (192.168.101.2) | Hosts file redirected `portal.meridian.local` to `198.51.100.77`. | Removed the redirect and preserved the original as `hosts.wardline.bak`. |
| win01 (192.168.101.3) | Hosts file redirected `records.meridian.local` and `ehr.meridian.local` to `198.51.100.77`. | Removed the redirects and preserved the original as `hosts.wardline.bak`. |

## Domain-resolution verification

Both Windows hosts were queried against app01 at `192.168.101.5`; the malicious redirects were removed and the resolver returned NXDOMAIN for the tested `dc01.meridian.local` name because app01 has no Meridian zone configured. The BIND process remains active and its configuration validates successfully. No Linux host-file hijacks were found. The scored WinRM, SMB, RDP, HTTP, DNS, and SQL services were left running.

## Residual risk

The internal `meridian.local` authoritative zone/records are absent from app01's BIND configuration. This should be reconciled with the domain DNS design after the event; adding guessed records during the scoring window could break domain authentication or name resolution.
