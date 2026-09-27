# Inject 4: Persistence inventory

## Linux

| Host | Mechanism | Artifact | Action |
|---|---|---|---|
| web01 | systemd enabled unit | `portal-healthd.service`; `EXFIL_URL=http://feed.wardline-telemetry.net/collect` | Preserved a root-owned evidence copy, disabled and stopped the unit; nginx was unmasked/started and returned HTTP 200. |
| app01 | systemd enabled units | `WardOptimizer.service`, `dns-cache-sync.service` | Preserved root-owned evidence copies, disabled and stopped both units. Apache2 and bind9 remained active. |
| db01 | cron persistence | `/etc/cron.d/cocoa-hidden-sync` running `/opt/.cocoa/cocoa-sync` every 15 minutes | Preserved a root-owned evidence copy and removed the cron entry. MariaDB remained active. |
| web01/db01 | covert process | `/usr/local/lib/.sysmon/beacon` with Wardline interval/target arguments | Terminated the running beacon processes; evidence path and command line were recorded during triage. |

The ordinary `backup` cron entry was observed on web01/app01 and was left in place pending separate validation because it is not an explicitly Wardline artifact. Scored services were checked after changes. `/etc/sudoers.d/zz-writable` permissions were corrected to 0440 and `visudo -cf /etc/sudoers` passed on all three Linux hosts.

## Windows

| Host | Mechanism | Artifact | Action |
|---|---|---|---|
| dc01 | non-standard services | `WARDLINE` (`C:\Windows\Temp\WrapWARDLINE.exe`), `MedArchiveSync`, `WardIndexer` (Wardline paths) | Stopped and removed from service configuration; WinRM was not modified. |
| win01 | scheduled tasks | `\PharmacyAudit`, `\TelemetryFlush`, both launching PowerShell from `C:\ProgramData\Wardline\` | Deleted both tasks. |
| win01 | non-standard services | `BedBoardSync`, `ChartCacheSvc`, `LabResultsSync` | Stopped and removed from service configuration; WinRM, SMB, and RDP were not modified. |

Protected scoring-check accounts were not changed. No reboot was performed.
