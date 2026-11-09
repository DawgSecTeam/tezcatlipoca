"""--packet fidelity gates: published credentials match credentials.txt; out-of-scope accounts exist."""

import subprocess

from verifier import context
from verifier.boxes import boxes_by_team, is_linux_box
from verifier.loaders import read_credentials_lines
from verifier.model import CheckError, gate_fail, gate_pass, gate_skip


def check_packet_creds(comp_dir, profile):
# --packet gate: every published default credential pair must match credentials.txt exactly —
#     in a packet-driven competition the packet IS the credential distribution.
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
# --packet gate: out-of-scope decoy accounts (scorebot/blackteam/red_scoring) must exist on
#     every team's first Linux box (probing only the first team's let a broken team pass);
#     unprovable (SSH dead) is a SKIP, not a pass — and a SKIP if ANY team stayed unprobed.
    users = list((profile.get("credentials") or {}).get("out_of_scope") or [])
    if not users:
        return gate_pass("packet_accounts", "packet declares no out-of-scope accounts")
    targets = []
    for identifier, team_boxes in sorted(boxes_by_team(boxes).items(), key=lambda kv: int(kv[0])):
        first = next((b for b in team_boxes if is_linux_box(b)), None)
        if first:
            targets.append(first)
    if not targets:
        print("  SKIP  — no Linux box to probe for out-of-scope accounts")
        return gate_skip("packet_accounts", "no Linux box to probe")
    probe = "; ".join(
        f"id -u {u} >/dev/null 2>&1 && echo {u}=1 || echo {u}=0" for u in users)
    failed, unprobed = {}, []
    for target in targets:
        label = target.get("name", target["ip"])
        try:
            proc = context.ssh_via_gateway(ctx, target["ip"], probe)
        except (CheckError, subprocess.TimeoutExpired) as e:
            print(f"  SKIP  — couldn't probe {label} ({e})")
            unprobed.append(label)
            continue
        if proc.returncode != 0:
            print(f"  SKIP  — probe of {label} failed rc={proc.returncode}: "
                  f"{(proc.stderr or '').strip()[:120]}")
            unprobed.append(label)
            continue
        kv = dict(l.split("=", 1) for l in proc.stdout.split() if "=" in l)
        missing = [u for u in users if kv.get(u) != "1"]
        if missing:
            print(f"  FAIL  out-of-scope account(s) missing on {label}: {', '.join(missing)}")
            failed[label] = missing
    if failed:
        return gate_fail("packet_accounts",
                         "; ".join(f"{box}: {', '.join(m)}" for box, m in failed.items()))
    if unprobed:
        return gate_skip("packet_accounts", f"couldn't probe {', '.join(unprobed)}")
    names = ", ".join(t.get("name", t["ip"]) for t in targets)
    print(f"  PASS  {len(users)} out-of-scope account(s) present on all "
          f"{len(targets)} team box(es) ({names})")
    return gate_pass("packet_accounts", f"{len(users)} account(s) present on {len(targets)} box(es)")
