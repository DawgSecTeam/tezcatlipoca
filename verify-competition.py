#!/usr/bin/env python3
"""Post-deploy verifier for a Quotient scoring range: logins, services, isolation, misconfig spot-check, injects."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import requests
    import urllib3
except ImportError as e:
    print(f"ERROR: missing dependency ({e}). Need: requests, python-dotenv.", file=sys.stderr)
    sys.exit(2)

from dotenv import load_dotenv

from utils import BOX_USERNAME_DEFAULT, load_users_config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO_ROOT = Path(__file__).resolve().parent
ENV_PATH = REPO_ROOT / ".env"

DEFAULT_ADMIN_PASSWORD = "changeme123"

MISCONFIG_CHECKS = {
    "suid-find": (
        "ls -l $(which find)",
        lambda out: "rws" in out.split("\n")[0],
    ),
    "www-data-shell": (
        "grep '^www-data:' /etc/passwd",
        lambda out: out.strip().endswith("/bin/bash"),
    ),
    "bad-perms-userConfig": (
        "stat -c %a /etc/shadow",
        lambda out: out.strip() == "666",
    ),
    "writable-sudoers": (
        "stat -c %a /etc/sudoers.d",
        lambda out: out.strip() == "777",
    ),
}


def _config_name(entry):
    """Normalize config entry (string or {"name": ...}) to name for dict lookup."""
    return entry if isinstance(entry, str) else entry.get("name")



class CheckError(Exception):
    """A check could not run (missing data / unreachable infra) — clean message, no trace."""


def read_terraform_ctx():
    """Read agent_context from `terraform output -json`; resolve ssh_key_path absolute."""

    try:
        raw = subprocess.run(
            ["terraform", "output", "-json"],
            cwd=str(REPO_ROOT / "terraform"),
            capture_output=True, text=True, check=True,
        ).stdout
    except FileNotFoundError:
        raise CheckError("terraform not found on PATH — pass --engine-ip to skip Terraform.")
    except subprocess.CalledProcessError as e:
        raise CheckError(f"`terraform output -json` failed: {e.stderr.strip() or e}")
    try:
        ctx = json.loads(json.loads(raw)["agent_context"]["value"])
    except (json.JSONDecodeError, KeyError) as e:
        raise CheckError(f"could not parse agent_context from terraform output: {e}")
    key_path = ctx.get("ssh_key_path")
    if key_path and not os.path.isabs(key_path):
        ctx["ssh_key_path"] = str((REPO_ROOT / "terraform" / key_path).resolve())
    return ctx


def resolve_ssh_key():
    """Resolve TF_VAR_ssh_private_key_path from .env, trying repo-root and terraform-relative."""
    val = os.environ.get("TF_VAR_ssh_private_key_path")
    if not val:
        return None
    for base in (REPO_ROOT, REPO_ROOT / "terraform"):
        cand = (base / val).resolve()
        if cand.exists():
            return str(cand)
    return str((REPO_ROOT / val).resolve())


def build_ctx(args, comp_dir):
    """Assemble connection context, honoring --engine-ip override."""
    ctx = {}
    tf_error = None
    if not args.engine_ip:
        ctx = read_terraform_ctx()
    else:
        try:
            ctx = read_terraform_ctx()
        except CheckError as e:
            tf_error = e
    if args.engine_ip:
        ctx["scoring_engine_ip"] = args.engine_ip
    if not ctx.get("scoring_engine_ip"):
        raise CheckError("no scoring_engine_ip (Terraform gave none and no --engine-ip).")
    if not ctx.get("ssh_key_path"):
        ctx["ssh_key_path"] = resolve_ssh_key()
    if not ctx.get("vm_username"):
        ctx["vm_username"] = os.environ.get("TF_VAR_vm_username")
    if not ctx.get("box_username"):
        ctx["box_username"] = load_users_config(comp_dir)[0] or BOX_USERNAME_DEFAULT
    if tf_error:
        print(f"  (note: Terraform context unavailable, using .env: {tf_error})")
    return ctx


def ssh_via_gateway(ctx, target_ip, cmd, timeout=60):
    """SSH to a target box via the scoring-engine gateway (mirrors create-competition.py)."""
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    box_username = ctx.get("box_username", BOX_USERNAME_DEFAULT)
    if not key or not scoring_user:
        raise CheckError("missing SSH key path or vm_username for gateway SSH.")
    proxy = (
        f"ssh -i {key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-W %h:%p {scoring_user}@{scoring_ip}"
    )
    try:
        return subprocess.run(
            [
                "ssh", "-i", key,
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=10",
                "-o", f"ProxyCommand={proxy}",
                f"{box_username}@{target_ip}", cmd,
            ],
            capture_output=True, text=True, timeout=timeout,
        )
    except OSError as e:
        raise CheckError(f"failed to run ssh: {e}")


def ssh_to_engine(ctx, cmd, timeout=30):
    """SSH directly to the scoring engine itself (no gateway hop — it's the gateway)."""
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    if not key or not scoring_user:
        raise CheckError("missing SSH key path or vm_username for engine SSH.")
    try:
        return subprocess.run(
            [
                "ssh", "-i", key,
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=10",
                f"{scoring_user}@{scoring_ip}", cmd,
            ],
            capture_output=True, text=True, timeout=timeout,
        )
    except OSError as e:
        raise CheckError(f"failed to run ssh: {e}")


def load_teams(comp_dir):
    path = comp_dir / "teams.json"
    if not path.exists():
        raise CheckError(f"teams.json not found: {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise CheckError(f"teams.json is not valid JSON: {e}")


def load_admin_password(comp_dir, override):
    if override:
        return override
    path = comp_dir / "credentials.txt"
    if path.exists():
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "admin":
                return parts[1]
    return DEFAULT_ADMIN_PASSWORD


_DEFAULT_CRED_LITERALS = {"changeme123", "password1", "password2", "ubuntu"}


def check_no_default_creds(comp_dir):
    """Confirm credentials.txt doesn't carry default literal passwords (rotation guard)."""
    path = comp_dir / "credentials.txt"
    if not path.exists():
        print("  (no credentials.txt to check for default creds)")
        return True
    box_lines = [l for l in path.read_text().splitlines() if l.startswith("box-")]
    if not box_lines:
        print("  (credentials.txt has no box-login/box-credlist lines — pre-rotation "
              "competition, or credential rotation isn't wired up)")
        return True
    bad = [l for l in box_lines if l.split()[-1] in _DEFAULT_CRED_LITERALS]
    if bad:
        print("  FAIL  credentials.txt still carries a default credential literal:")
        for l in bad:
            print(f"      {l}")
        return False
    print(f"  PASS  {len(box_lines)} box credential line(s), no default literals found")
    return True


def count_local_injects(comp_dir):
    """Number of inject subdirectories (each carrying an inject.json). 0 if no injects/ dir."""
    injects_dir = comp_dir / "injects"
    if not injects_dir.is_dir():
        return None
    return sum(1 for sub in injects_dir.iterdir() if sub.is_dir() and (sub / "inject.json").exists())


def load_boxes(comp_dir):
    """Load boxes from per-competition nakon-config.json."""
    path = comp_dir / "nakon-config.json"
    if not path.exists():
        legacy = REPO_ROOT / "nakon" / "config.json"
        hint = (f"\n(nakon/config.json still exists at {legacy} — it predates the move to "
                f"per-competition machine lists; re-run create-competition.py to regenerate)"
                if legacy.exists() else "")
        raise CheckError(f"nakon-config.json not found: {path}{hint}")
    try:
        return json.loads(path.read_text()).get("machines", [])
    except json.JSONDecodeError as e:
        raise CheckError(f"{path} is not valid JSON: {e}")



def check_logins(base_url, teams, admin_password):
    """POST /api/login for admin + every team. Returns (all_ok, admin_session)."""
    print("\n[1/5] LOGIN")
    all_ok = True

    def try_login(username, password):
        session = requests.Session()
        try:
            r = session.post(f"{base_url}/api/login",
                             json={"username": username, "password": password}, timeout=10)
        except requests.RequestException as e:
            print(f"  FAIL  {username:<10} — request error: {e}")
            return None
        ok = r.status_code == 200
        print(f"  {'PASS' if ok else 'FAIL'}  {username:<10} — HTTP {r.status_code}")
        return session if ok else None

    admin_session = try_login("admin", admin_password)
    if admin_session is None:
        all_ok = False
    for team_name, data in teams.items():
        if try_login(team_name, data.get("password", "")) is None:
            all_ok = False
    return all_ok, admin_session


def _check_passed(check):
    """Check Result truthiness with string normalization ("false"/"0" -> False)."""
    result = check.get("Result")
    if isinstance(result, str):
        return result.strip().lower() not in ("", "0", "false")
    return bool(result)


def check_services(base_url, admin_session, teams, strict):
    """Report per-team service UP/DOWN from the latest scored round. Returns (query_ok, all_up)."""
    print("\n[2/5] SERVICES")
    if admin_session is None:
        print("  SKIP  — no admin session (login failed).")
        return False, False
    try:
        r = admin_session.get(f"{base_url}/api/teams", timeout=10)
        r.raise_for_status()
        api_teams = r.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"  FAIL  — could not fetch /api/teams: {e}")
        return False, False

    query_ok = True
    all_up = True
    any_service = False
    for t in api_teams:
        tid, tname = t.get("ID"), t.get("Name", t.get("Identifier"))
        try:
            r = admin_session.get(f"{base_url}/api/services/{tid}", timeout=10)
            r.raise_for_status()
            services = r.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            print(f"  WARN  {tname}: could not fetch services: {e}")
            query_ok = False
            all_up = False
            continue
        up = 0
        unscored = 0
        down_names = []
        for svc in services or []:
            any_service = True
            name = svc.get("ServiceName", "?")
            rounds = svc.get("Last10Rounds") or []
            first_round = rounds[0] if rounds else None
            checks = (first_round.get("Checks") if isinstance(first_round, dict) else None) or []
            if not checks:
                unscored += 1
                continue
            if all(_check_passed(c) for c in checks):
                up += 1
            else:
                down_names.append(name)
        total = len(services or []) - unscored
        note = f" ({unscored} not yet scored)" if unscored else ""
        print(f"  {tname}: {up}/{total} services UP{note}")
        for name in down_names:
            print(f"      DOWN: {name}")
        if down_names:
            all_up = False
    if not any_service:
        print("  (no services reported yet — engine may not have scored a round)")
    if strict and not all_up:
        print("  --strict-services: some services DOWN -> counts against exit code")
    return query_ok, all_up


def check_isolation(ctx, teams, boxes):
    """Confirm isolation DROP rule present and actually blocks cross-team traffic."""

    print("\n[3/5] ISOLATION")
    try:
        proc = ssh_to_engine(ctx, "sudo iptables -S FORWARD")
    except CheckError as e:
        print(f"  FAIL  — {e}")
        return False
    if proc.returncode != 0:
        print(f"  FAIL  — could not read FORWARD chain (rc={proc.returncode}): "
              f"{(proc.stderr or '').strip()[:150]}")
        return False
    has_drop_rule = any(
        "-j DROP" in line and "192.168.0.0/16" in line
        and line.count("192.168.0.0/16") >= 2
        for line in proc.stdout.splitlines()
    )
    if not has_drop_rule:
        print("  FAIL  — no 192.168.0.0/16 -> 192.168.0.0/16 DROP rule in FORWARD chain; "
              "teams can currently route to each other through the engine.")
        return False
    print("  PASS  isolation DROP rule present in FORWARD chain")

    if len(teams) < 2:
        print("  (only 1 team — skipping the cross-team connection test)")
        return True

    identifiers = sorted({str(t["identifier"]) for t in teams.values()})
    team_ips = {}
    for box in boxes:
        parts = box.get("ip", "").split(".")
        if len(parts) == 4 and parts[2] in identifiers and parts[2] not in team_ips:
            team_ips[parts[2]] = box["ip"]
    if len(team_ips) < 2:
        print("  (couldn't identify 2 distinct teams' boxes from nakon-config.json — skipping "
          "the cross-team connection test)")
        return True
    from_ip, to_ip = (team_ips[i] for i in sorted(team_ips)[:2])

    try:
        proc = ssh_via_gateway(
            ctx, from_ip,
            f"timeout 3 bash -c 'echo > /dev/tcp/{to_ip}/22' 2>/dev/null; echo RC=$?"
        )
    except (CheckError, subprocess.TimeoutExpired) as e:
        print(f"  WARN  — cross-team connection test couldn't run ({e}); rule-presence check "
              "above already passed, not failing on this alone.")
        return True

    if proc.returncode != 0:
        print(f"  WARN  — couldn't SSH to {from_ip} to run the test "
              f"(rc={proc.returncode}): {(proc.stderr or '').strip()[:150]}; rule-presence "
              "check above already passed, not failing on this alone.")
        return True

    blocked = "RC=0" not in proc.stdout

    if blocked:
        print(f"  PASS  {from_ip} cannot reach {to_ip}:22 (blocked as expected)")
    else:
        print(f"  FAIL  {from_ip} CAN reach {to_ip}:22 — the isolation rule isn't actually "
              "blocking traffic (shadowed or misordered in FORWARD?)")

    try:
        proc2 = ssh_via_gateway(
            ctx, from_ip,
            "timeout 3 bash -c 'echo > /dev/tcp/1.1.1.1/443' 2>/dev/null; echo RC=$?"
        )
        internet_ok = "RC=0" in proc2.stdout
    except (CheckError, subprocess.TimeoutExpired):
        internet_ok = None
    if internet_ok is False:
        print(f"  WARN  {from_ip} can't reach the internet either — the rule (or NAT) may be "
              "over-blocking, not just isolating teams")
    elif internet_ok is None:
        print("  WARN  couldn't run the internet-reachability control check")
    else:
        print(f"  ....  control check ok: {from_ip} can still reach the internet")

    return blocked


def report_healthcheck_status(ctx):
    """Report range-healthcheck.timer status (informational, not gate)."""
    try:
        proc = ssh_to_engine(
            ctx,
            "systemctl is-active range-healthcheck.timer 2>&1; "
            "echo ---; tail -n 5 /var/log/range-healthcheck.log 2>/dev/null"
        )
    except CheckError as e:
        print(f"  (couldn't check range-healthcheck.timer: {e})")
        return
    if proc.returncode != 0 and "inactive" not in proc.stdout and "active" not in proc.stdout:
        print(f"  (couldn't check range-healthcheck.timer: {(proc.stderr or '').strip()[:150]})")
        return
    parts = proc.stdout.split("---", 1)
    status = parts[0].strip()
    tail = parts[1].strip() if len(parts) > 1 else ""
    print(f"  range-healthcheck.timer: {status or 'unknown'}")
    if tail:
        print("  recent log entries:")
        for line in tail.splitlines():
            print(f"      {line}")
    elif status == "active":
        print("  (no recent failures logged)")


def check_misconfig(ctx, boxes):
    """SSH via gateway to one box and confirm >=1 planted misconfig. Returns bool."""
    print("\n[4/5] MISCONFIG SPOT-CHECK")
    target = None
    for box in boxes:
        verifiable = [c for c in map(_config_name, box.get("configurations", []))
                      if c in MISCONFIG_CHECKS]
        if box.get("ip") and verifiable:
            target = (box, verifiable)
            break
    if target is None:
        print("  FAIL  — no box in nakon-config.json carries a verifiable misconfig.")
        return False
    box, verifiable = target
    ip = box["ip"]
    print(f"  target: {box.get('name', ip)} ({ip}) — candidates: {', '.join(verifiable)}")
    for config in verifiable:
        cmd, predicate = MISCONFIG_CHECKS[config]
        try:
            proc = ssh_via_gateway(ctx, ip, cmd)
        except subprocess.TimeoutExpired:
            print(f"  WARN  {config}: SSH timed out.")
            continue
        except CheckError as e:
            print(f"  FAIL  — {e}")
            return False
        if proc.returncode != 0:
            print(f"  WARN  {config}: command failed (rc={proc.returncode}): "
                  f"{(proc.stderr or '').strip()[:120]}")
            continue
        out = proc.stdout
        if predicate(out):
            print(f"  PASS  confirmed '{config}' — `{cmd}` -> {out.strip()[:80]}")
            return True
        print(f"  ....  '{config}' not present: {out.strip()[:80]}")
    print("  FAIL  — no planted misconfig could be confirmed on the target box.")
    return False


def report_beacons(ctx, machines):
    """Report planted raw-socket beacons (informational, not a gate)."""
    print("\n  (team beacons — informational)")
    expected = live = 0
    for m in machines or []:
        if "win" in (m.get("os") or "").lower():
            continue
        expected += 1
        ip = m.get("ip")
        name = m.get("name", ip)
        try:
            proc = ssh_via_gateway(
                ctx, ip, "systemctl is-active wda-digest.service 2>/dev/null || true", timeout=30)
        except (subprocess.TimeoutExpired, CheckError) as e:
            print(f"  WARN  {name} ({ip}): unreachable ({e})")
            continue
        state = (proc.stdout or "").strip()
        if state == "active":
            live += 1
            print(f"  LIVE  {name} ({ip}) — wda-digest.service active")
        else:
            print(f"  ....  {name} ({ip}) — no beacon unit running ({state or 'none'})")
    print(f"  beacons live: {live}/{expected} linux boxes")


def check_misconfig_survival(ctx, boxes):
    """Confirm every team's copy of each box carries same verifiable misconfigs (clone race guard)."""
    print("\n  (cross-team misconfig survival check)")
    groups = {}
    for box in boxes:
        if not box.get("ip"):
            continue
        configs = tuple(_config_name(c) for c in box.get("configurations", []))
        verifiable = [c for c in configs if c in MISCONFIG_CHECKS]
        if not verifiable:
            continue
        groups.setdefault(configs, []).append(box)

    multi_team_groups = [(configs, machines) for configs, machines in groups.items()
                          if len(machines) > 1]
    if not multi_team_groups:
        print("  SKIP  — fewer than 2 teams, or no box's misconfigs are both shared and "
              "verifiable.")
        return True

    all_ok = True
    for configs, machines in multi_team_groups:
        verifiable = [c for c in configs if c in MISCONFIG_CHECKS]
        for config in verifiable:
            cmd, predicate = MISCONFIG_CHECKS[config]
            present, absent, unknown = [], [], []
            for m in machines:
                name = m.get("name", m["ip"])
                try:
                    proc = ssh_via_gateway(ctx, m["ip"], cmd)
                except (subprocess.TimeoutExpired, CheckError):
                    unknown.append(name)
                    continue
                if proc.returncode != 0:
                    unknown.append(name)
                    continue
                (present if predicate(proc.stdout) else absent).append(name)
            if present and absent:
                all_ok = False
                print(f"  FAIL  '{config}' present on {present} but MISSING on {absent} — "
                      f"didn't survive cloning")
            elif present and not absent:
                note = f" (unverified: {unknown})" if unknown else ""
                print(f"  PASS  '{config}' present on all {len(present)} team(s): {present}{note}")
            elif unknown and not present and not absent:
                print(f"  WARN  '{config}' — could not verify on any team ({unknown})")
    return all_ok


def check_injects(base_url, admin_session, comp_dir):
    """Compare engine inject count to the competition's injects/ dir. Returns (relevant, ok)."""
    print("\n[5/5] INJECTS")
    expected = count_local_injects(comp_dir)
    if expected is None:
        print("  SKIP  — competition ships no injects/ dir.")
        return False, True
    if admin_session is None:
        print("  FAIL  — no admin session to query /api/injects.")
        return True, False
    try:
        r = admin_session.get(f"{base_url}/api/injects", timeout=10)
        r.raise_for_status()
        injects = r.json() or []
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"  FAIL  — could not fetch /api/injects: {e}")
        return True, False
    for inj in injects:
        title = inj.get("Title") or inj.get("title") or "?"
        print(f"      - {title}")
    ok = len(injects) == expected
    print(f"  {'PASS' if ok else 'FAIL'}  engine has {len(injects)} inject(s), "
          f"expected {expected} from injects/ dir")
    return True, ok



def main():
    parser = argparse.ArgumentParser(description="Verify a deployed Quotient competition range.")
    parser.add_argument("comp_dir", help="path to competitions/<id>")
    parser.add_argument("--engine-ip", help="override the scoring-engine IP (skip Terraform)")
    parser.add_argument("--admin-password", help="override the Quotient admin password")
    parser.add_argument("--strict-services", action="store_true",
                        help="also require every service UP for a passing exit code")
    args = parser.parse_args()

    load_dotenv(ENV_PATH)

    comp_dir = Path(args.comp_dir).resolve()
    if not comp_dir.is_dir():
        print(f"ERROR: competition directory not found: {comp_dir}", file=sys.stderr)
        return 2

    try:
        ctx = build_ctx(args, comp_dir)
        teams = load_teams(comp_dir)
        admin_password = load_admin_password(comp_dir, args.admin_password)
        boxes = load_boxes(comp_dir)
    except CheckError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    engine_ip = ctx["scoring_engine_ip"]
    base_url = f"http://{engine_ip}"
    print(f"Verifying competition '{comp_dir.name}' against engine {base_url}")

    logins_ok, admin_session = check_logins(base_url, teams, admin_password)
    print("\n  (default-credential regression guard)")
    no_default_creds_ok = check_no_default_creds(comp_dir)
    services_query_ok, services_all_up = check_services(base_url, admin_session, teams,
                                                         args.strict_services)
    isolation_ok = check_isolation(ctx, teams, boxes)
    print("\n  (live-ops health check status — informational)")
    report_healthcheck_status(ctx)
    misconfig_ok = check_misconfig(ctx, boxes)
    misconfig_survival_ok = check_misconfig_survival(ctx, boxes)
    report_beacons(ctx, boxes)
    injects_relevant, injects_ok = check_injects(base_url, admin_session, comp_dir)

    gate = {
        "logins": logins_ok,
        "no_default_creds": no_default_creds_ok,
        "isolation": isolation_ok,
        "misconfig": misconfig_ok,
        "misconfig_survival": misconfig_survival_ok,
        "injects": injects_ok,
    }
    if args.strict_services:
        gate["services(strict)"] = services_query_ok and services_all_up

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  logins           : {'PASS' if logins_ok else 'FAIL'}")
    print(f"  no_default_creds : {'PASS' if no_default_creds_ok else 'FAIL'}")
    svc_note = "informational"
    if args.strict_services:
        svc_note = "PASS" if (services_query_ok and services_all_up) else "FAIL"
    print(f"  services         : {'UP' if services_all_up else 'some DOWN'} "
          f"({'query ok' if services_query_ok else 'query failed'}) [{svc_note}]")
    print(f"  isolation        : {'PASS' if isolation_ok else 'FAIL'}")
    print(f"  misconfig        : {'PASS' if misconfig_ok else 'FAIL'}")
    print(f"  misconfig_surviv.: {'PASS' if misconfig_survival_ok else 'FAIL'}")
    print(f"  injects          : {'PASS' if injects_ok else 'FAIL'}"
          f"{'' if injects_relevant else ' (none — skipped)'}")

    passed = all(gate.values())
    print("\n" + ("RESULT: PASS — competition looks healthy."
                  if passed else "RESULT: FAIL — see failing checks above."))
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
