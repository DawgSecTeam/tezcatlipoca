"""Reset-request sync to the run-queue dashboard (the notification bell's source).

Enabled when TEZ_OPS_URL + TEZ_OPS_TOKEN are set — portal_ops writes both into the
portal's .env only when the DASHBOARD ran the deploy, so a dashboard-deployed range
gets the channel and an ssh-deployed one silently doesn't (requests still record
locally). One converging call: the portal posts its whole request list, the dashboard
answers with the authoritative status per id (it is the resolution authority — an
operator's Approve/Deny/reset-run outcome must not be overwritten locally). The
dashboard being down must never take the portal down: failures retry next tick.
"""

import threading
import time

import requests

INTERVAL = 15


def sync_once(store, base_url, token):
    rows = store.all()
    r = requests.post(f"{base_url.rstrip('/')}/api/ops/requests", timeout=10,
                      headers={"Authorization": f"Bearer {token}"},
                      json={"requests": rows})
    r.raise_for_status()
    statuses = (r.json() or {}).get("statuses") or {}
    for row in rows:
        remote = statuses.get(str(row["id"]))
        if remote and remote != row["status"]:
            store.set_status(row["id"], remote)


def start(store, base_url, token, interval=INTERVAL):
    def loop():
        while True:
            try:
                sync_once(store, base_url, token)
            except Exception:  # noqa: BLE001 - the dashboard being down is not fatal here
                pass
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True, name="ops-sync").start()
