#!/usr/bin/env python3
"""Inspect the target catalog rows from the catalog dump already inside the VM.

Run inside the vulndb VM (vmid 115). Reads /tmp/catalog.json (written by
`vulndb_cli list --json`), prints metadata for the defect targets, and writes the
selected rows to /tmp/catalog-before.json for transfer back to the workstation.
"""
import json

TARGETS = [
    "tftpd-hpa-anon-write",
    "postgresql-remote-access",
    "postgresql-no-auth",
    "sshd-force-sftp-broken-chroot",
    "unrealircd-backdoor-container",
    "local-user",
    "local-user-win",
    "powershell-execution-unrestricted",
    "rpc-proxy-on-dc-web-win",
    "unauth-kiosk-app-startup-win",
    "mailenable-cleartext-mail-win",
]

catalog = json.load(open("/tmp/catalog.json"))
by_name = {c["name"]: c for c in catalog}
selected = [by_name[t] for t in TARGETS if t in by_name]
json.dump(selected, open("/tmp/catalog-before.json", "w"), indent=1)

print("found %d of %d targets" % (len(selected), len(TARGETS)))
for c in selected:
    print("  %-34s id=%-4s platform=%-10s type=%-11s run_as=%-6s depends_on=%s len=%d" % (
        c["name"], c["id"], c.get("platform"), c.get("type"), c.get("run_as"),
        c.get("depends_on"), len(c.get("script") or "")))
missing = [t for t in TARGETS if t not in by_name]
print("MISSING:", missing)
