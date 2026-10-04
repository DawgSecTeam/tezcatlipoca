"""--packet fidelity gates: published credentials match credentials.txt; out-of-scope accounts exist."""

import subprocess

from verifier import context
from verifier.boxes import is_linux_box
from verifier.loaders import read_credentials_lines
from verifier.model import CheckError, gate_fail, gate_pass, gate_skip


def check_packet_creds(comp_dir, profile):
    """--packet gate: credentials.txt must carry the packet's published default
    credentials verbatim. In a packet-driven competition the packet IS the credential
    distribution — teams rotate at minute zero — so the guard inverts: instead of
    rejecting known-default literals, every published pair must match exactly."""
    creds = profile.get("credentials") or {}
    credlists = creds.get("credlists") or {}
    expected_pw = creds.get("box_password")
    lines = read_credentials_lines(comp_dir)
    if lines is None:
        print("  FAIL  — no credentials.txt to check against the packet")
        return False
    ok = True
    checked = 0
    if expected_pw:
        login = next((l for l in lines if l.startswith("box-login")), None)
        got = login.split()[-1] if login else None
        if got != str(expected_pw):
            print(f"  FAIL  box-login password != packet default "
                  f"(for `{creds.get('box_username')}`)")
            ok = False
        checked += 1
    for prefix, users in (("box-credlist-", credlists.get("linux") or {}),
                          ("box-credlist-domain-", credlists.get("domain") or {})):
        for user, pw in users.items():
            line = next((l for l in lines if l.startswith(f"{prefix}{user} ")), None)
            got = line.split()[-1] if line else None
            if got != str(pw):
                print(f"  FAIL  credlist account `{user}` "
                      f"({'domain' if 'domain' in prefix else 'local'}) "
                      "password != packet default")
                ok = False
            checked += 1
    if ok:
        print(f"  PASS  {checked} packet-published credential(s) match credentials.txt")
    return ok


def check_packet_accounts(ctx, profile, boxes):
    """--packet gate: out-of-scope decoy accounts (scorebot/blackteam/red_scoring) exist
    on a Linux box. The packet promises these accounts exist and stay untouched — teams
    enumerate local accounts in minute-zero IR, and a missing decoy breaks that promise.
    Unprovable (SSH dead) is a SKIP, not a pass — the old code returned True on every
    unprovable path, so a dead SSH exited 0 (live-found 2026-10-02)."""
    users = list((profile.get("credentials") or {}).get("out_of_scope") or [])
    if not users:
        return gate_pass("packet_accounts", "packet declares no out-of-scope accounts")
    linux_boxes = [b for b in boxes if is_linux_box(b)]
    if not linux_boxes:
        print("  SKIP  — no Linux box to probe for out-of-scope accounts")
        return gate_skip("packet_accounts", "no Linux box to probe")
    target = linux_boxes[0]
    probe = "; ".join(
        f"id -u {u} >/dev/null 2>&1 && echo {u}=1 || echo {u}=0" for u in users)
    try:
        proc = context.ssh_via_gateway(ctx, target["ip"], probe)
    except (CheckError, subprocess.TimeoutExpired) as e:
        print(f"  SKIP  — couldn't probe {target.get('name', target['ip'])} ({e})")
        return gate_skip("packet_accounts", f"couldn't probe {target.get('name', target['ip'])}")
    if proc.returncode != 0:
        print(f"  SKIP  — probe failed rc={proc.returncode}: "
              f"{(proc.stderr or '').strip()[:120]}")
        return gate_skip("packet_accounts", f"probe failed rc={proc.returncode}")
    kv = dict(l.split("=", 1) for l in proc.stdout.split() if "=" in l)
    missing = [u for u in users if kv.get(u) != "1"]
    if missing:
        print(f"  FAIL  out-of-scope account(s) missing on "
              f"{target.get('name', target['ip'])}: {', '.join(missing)}")
        return gate_fail("packet_accounts",
                     f"missing on {target.get('name', target['ip'])}: {', '.join(missing)}")
    print(f"  PASS  {len(users)} out-of-scope account(s) present on "
          f"{target.get('name', target['ip'])}")
    return gate_pass("packet_accounts", f"{len(users)} account(s) present")
