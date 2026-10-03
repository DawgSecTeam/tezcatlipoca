# Outbound Beacon and Redirect Kill Sheet

## Evidence collected

| Host | Artifact | Evidence | Action |
|---|---|---|---|
| web01 | `portal-healthd.service` | Enabled and active; `/etc/systemd/system/portal-healthd.service` contained `Environment=EXFIL_URL=http://feed.wardline-telemetry.net/collect` and an idle shell loop | Disabled, stopped, removed unit file, and reloaded systemd |
| app01 | `WardOptimizer.service` | Enabled and active from `/etc/systemd/system/WardOptimizer.service`; suspicious optimizer daemon with an idle shell loop | Disabled, stopped, removed unit file, and reloaded systemd |
| dc01 | `C:\ProgramData\Wardline\dsync.ps1`, `ledger.ps1` scheduled actions | Scheduled-task inventory showed PowerShell with `-ExecutionPolicy Bypass` from `C:\ProgramData\Wardline\` | Stopped and unregistered matching tasks |
| dc01 | `WARDLINE` service | Auto-start, running as `LocalSystem`, binary `C:\Windows\Temp\WrapWARDLINE.exe` | Stopped and deleted service |
| dc01 | Run/RunOnce keys | `PulseSyncAgent`, `TelemetrySync`, and `TelemetrySync` RunOnce launched Wardline scripts | Removed entries |
| win01 | `TelemetryFlush` scheduled task and Wardline Run entries | Task launched `C:\ProgramData\Wardline\tflush.ps1`; Run entries launched `ncall.ps1` and `badge.ps1` | Removed matching task and Run entries |
| dc01 | hosts redirect | `198.51.100.77 portal.meridian.local` | Removed redirect |
| win01 | hosts redirects | `198.51.100.77 records.meridian.local` and `ehr.meridian.local` | Removed redirects |

## Verification

- Linux suspicious units report `inactive` and `not-found`; no `198.51.100.77` hosts entries remain.
- Windows verification reports zero Wardline PowerShell scheduled tasks, no `WARDLINE` service, zero redirect entries, and no matching Run entries.
- Scored services were checked after remediation. `web01-http` briefly reported down because nginx was masked/stopped; nginx was unmasked and started immediately. Other scored checks remained UP.

No engine access or scoring-check account changes were performed.
