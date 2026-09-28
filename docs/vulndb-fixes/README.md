# vulndb catalog fixes (applied to the live `vulns` DB, tracked here)

The nakon catalog (`configurations` table in the vulndb MySQL) is a shared service, not a repo
file. Script-body fixes applied there are recorded here so they are reviewable and re-appliable.
Apply with an `UPDATE configurations SET script=<file> WHERE name=<name> AND platform=<platform>`.

| name | platform | file | why | applied |
|---|---|---|---|---|
| `local-user` | linux | `local-user-linux.sh` | `usermod -a -G sudo` failed rc=6 on Fedora (no `sudo` group; RHEL/Fedora use `wheel`). Now maps `sudo`↔`wheel` to whatever the box has and creates any other missing group — distro-portable. | 2026-09-28 |
