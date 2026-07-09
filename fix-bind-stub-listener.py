#!/usr/bin/env python3
"""
Patches the 'bind' service config in the Nakon database to disable
systemd-resolved's stub listener before starting bind9.

On Debian 13, systemd-resolved listens on 127.0.0.53:53 by default.
bind9 tries to bind 0.0.0.0:53 (all interfaces), which covers that same
address, so one of them fails. In practice systemd-resolved starts first
at boot, claims 127.0.0.53:53, and bind9 fails to start.
"""
import os
from pathlib import Path
from dotenv import load_dotenv
import mysql.connector

load_dotenv(Path("nakon/.env"))
db = mysql.connector.connect(
    host=os.getenv("host"), user=os.getenv("user"),
    password=os.getenv("password"), database=os.getenv("database"),
)
cursor = db.cursor(dictionary=True)
cursor.execute("SELECT id, script FROM configurations WHERE name = 'bind'")
row = cursor.fetchone()
if not row:
    print("ERROR: no 'bind' config found in the database")
    raise SystemExit(1)

NEW_SCRIPT = r"""#!/bin/bash
set -e
if command -v apt-get > /dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y bind9 bind9utils bind9-doc
    mkdir -p /etc/systemd/resolved.conf.d
    printf '[Resolve]\nDNSStubListener=no\n' > /etc/systemd/resolved.conf.d/no-stub.conf
    systemctl restart systemd-resolved 2>/dev/null || true
    systemctl enable bind9 && systemctl start bind9
elif command -v dnf > /dev/null; then
    dnf install -y bind bind-utils
    systemctl enable named && systemctl start named
elif command -v yum > /dev/null; then
    yum install -y bind bind-utils
    systemctl enable named && systemctl start named
else
    echo "[bind] No supported package manager found" >&2; exit 1
fi
echo "[bind] Done"
"""

cursor.execute("UPDATE configurations SET script = %s WHERE id = %s", (NEW_SCRIPT, row["id"]))
db.commit()
print(f"Updated bind script (id={row['id']})")
cursor.close()
db.close()
