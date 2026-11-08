"""Pre-T0 proof that declared feeds are actually indexing in each team's own tools.

run-14ac7f14: provisioning looked complete, and the event nearly fired with ZERO
Windows events on every head — UFs had been stripped pre-T0, the index existed but
held nothing, and no gate could notice. A comp declares what must be flowing in
telemetry.json; each entry asks the team's own box to query its local ingest API
(REST oneshot) over a freshness window and read a count field back. No telemetry.json
= nothing declared = informational skip (comps without a log pipeline stay verifiable).

Schema (competitions/<comp>/telemetry.json) — credentials here must be TEAM-KNOWN
packet-grade values; this file rides in the competition repo:

  {"expected": [
     {"box": "splunk01", "label": "windows_evtlog fresh",
      "url": "https://localhost:8089/services/search/jobs/oneshot",
      "auth_user": "admin", "auth_password": "changeme123!",
      "params": {"search": "search index=windows_evtlog | stats count as n",
                 "earliest_time": "-15m", "output_mode": "json"},
      "count_field": "n", "min_count": 1}]}
"""

import json
import shlex
from pathlib import Path

from verifier import context
from verifier.model import CheckError, bool_gate, gate_skip


def load_expectations(comp_dir):
    p = Path(comp_dir) / "telemetry.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except ValueError as e:
        raise CheckError(f"telemetry.json unreadable: {e}")
    return data.get("expected") or []


# the curl one-liner executed ON the team's box (as the box user — splunk REST is the
# door; the filesystem under /opt/splunk is root-owned by design)
def probe_cmd(e):
    argv = ["curl", "-sk", "-m", "25",
            "-u", f"{e.get('auth_user', 'admin')}:{e.get('auth_password', '')}"]
    for key, value in (e.get("params") or {}).items():
        argv += ["--data-urlencode", f"{key}={value}"]
    argv.append(e["url"])
    return " ".join(shlex.quote(a) for a in argv)


def _count_in(body, field):
    data = json.loads(body)
    results = data.get("results") or []
    if not results:
        return 0
    return int(float(results[0].get(field, 0)))


def check_telemetry(ctx, comp_dir, teams, boxes):
    expected = load_expectations(comp_dir)
    if not expected:
        return gate_skip("telemetry", "no telemetry.json — the comp declares no ingest "
                         "expectations", gating=False)
    octet_by_box = {b["name"]: b.get("last_octet") for b in boxes}
    failures, probes = [], 0
    for team_key, tdata in teams.items():
        ident = tdata.get("identifier") if isinstance(tdata, dict) else tdata
        for e in expected:
            octet = octet_by_box.get(e["box"])
            label = f"{team_key}:{e.get('label', e['box'])}"
            if not ident or octet is None:
                failures.append(f"{label} (box/identifier unresolved)")
                continue
            probes += 1
            ip = f"192.168.{ident}.{octet}"
            try:
                proc = context.ssh_via_gateway(ctx, ip, probe_cmd(e), timeout=45)
                count = _count_in(proc.stdout or "", e.get("count_field", "n"))
            except CheckError as e2:
                failures.append(f"{label} ({e2})")
                continue
            except (ValueError, KeyError) as e2:
                failures.append(f"{label} (probe answer unreadable: {e2})")
                continue
            if count < int(e.get("min_count", 1)):
                failures.append(f"{label} count={count}")
    if failures:
        return bool_gate("telemetry", False,
                         f"{len(failures)}/{probes} feeds not fresh — "
                         + "; ".join(failures[:4])[:220])
    return bool_gate("telemetry", True, f"{probes} declared feed(s) fresh across "
                                        f"{len(teams)} team(s)")
