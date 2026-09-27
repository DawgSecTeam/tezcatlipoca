# Inject 6: Outbound beacon and redirect kill sheet

## Findings

| Host | Artifact and evidence | Action |
|---|---|---|
| web01 | `/usr/local/lib/.sysmon/beacon -t 192.168.102.1 -p 4444 -i 45 -n 0 -b wardline-web01`; systemd unit `/etc/systemd/system/wda-digest.service` was enabled, active, and configured with `Restart=always`. Journal showed SYN beacons every 45 seconds. | Preserved the unit file under `/root/wardline-evidence/inject6/`; stopped and disabled `wda-digest.service`; confirmed it is inactive, disabled, and no beacon process remains. HTTP service was not changed. |
| app01 | No matching telemetry, health, beacon, sysmon, or Wardline process/unit; `/etc/hosts` had no attacker redirect. | No change. Apache and bind were left running. |
| db01 | `/usr/local/lib/.sysmon/beacon -t 192.168.102.1 -p 4444 -i 90 -n 0 -b wardline-db01`; systemd unit `/etc/systemd/system/wda-digest.service` was enabled, active, and configured with `Restart=always`. Journal showed SYN beacons every 90 seconds. | Preserved the unit file under `/root/wardline-evidence/inject6/`; stopped and disabled `wda-digest.service`; confirmed it is inactive, disabled, and no beacon process remains. MariaDB was not changed. |
| dc01 | `C:\Windows\System32\drivers\etc\hosts` contained `198.51.100.77 portal.meridian.local` and `198.51.100.77 lab.meridian.local`. | Removed only the two `198.51.100.77` mappings. WinRM was not changed. |
| win01 | `C:\Windows\System32\drivers\etc\hosts` contained `198.51.100.77 records.meridian.local`, `ehr.meridian.local`, and `pharmacy.meridian.local`. | Removed only the three `198.51.100.77` mappings. WinRM, SMB, and RDP were not changed. |

## Containment validation

- Rechecked Linux process lists: no `/usr/local/lib/.sysmon/beacon` process remains on web01 or db01.
- Rechecked the Linux and Windows hosts files: no mapping to `198.51.100.77` remains.
- All eight scored services continued passing after containment; no protected scoring-check account was changed.
