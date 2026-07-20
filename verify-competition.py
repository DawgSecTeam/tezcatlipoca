#!/usr/bin/env python3
"""Post-deploy verifier for a Quotient scoring range competition.

Reproduces the manual smoke-checks we run by hand after a deploy:

  1. LOGIN      — every team account and admin can POST /api/login (HTTP 200).
  2. SERVICES   — each team's services report UP in the latest scored round (informational).
  3. MISCONFIG  — at least one planted misconfig is present on a target box (SSH via gateway).
  4. INJECTS    — if the competition ships an injects/ dir, the engine has that many injects.

Usage:
    python3 verify-competition.py competitions/<id> [options]

Exit code is 0 only when logins all pass, the misconfig spot-check confirms, and injects
(if any) are present. Service DOWN is reported but is NOT fatal unless --strict-services.

Data sources (all read at runtime, nothing hardcoded except sane fallbacks):
  - engine IP        : `terraform output -json` .agent_context.value.scoring_engine_ip
                       (override with --engine-ip)
  - team creds       : competitions/<id>/teams.json
  - admin password   : competitions/<id>/credentials.txt  (fallback: changeme123,
                       override with --admin-password)
  - ssh key / user   : .env  TF_VAR_ssh_private_key_path / TF_VAR_vm_username
  - planted configs  : nakon/config.json
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import requests
    import urllib3
except ImportError as e:  # pragma: no cover
    print(f"ERROR: missing dependency ({e}). Need: requests, python-dotenv.", file=sys.stderr)
    sys.exit(2)

from dotenv import load_dotenv

# The engine speaks plain HTTP, but Quotient's checks & the Proxmox API elsewhere use
# self-signed TLS — silence the noise so output stays readable.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO_ROOT = Path(__file__).resolve().parent
ENV_PATH = REPO_ROOT / ".env"

# Box login (see utils.py BOX_USERNAME / BOX_PASSWORD). We authenticate to boxes with the
# proxmox key (cloud-init authorizes it for 'ubuntu'), so the password is informational.
BOX_USERNAME = "ubuntu"
DEFAULT_ADMIN_PASSWORD = "changeme123"

# Planted-misconfig verifiers: config name -> (shell command, predicate on stdout).
# Only a handful are mapped; the spot-check picks a box/config combination it can verify.
MISCONFIG_CHECKS = {
    # SUID bit set on find -> `ls -l` shows the 's' in the owner-exec slot (e.g. -rwsr-xr-x).
    "suid-find": (
        "ls -l $(which find)",
        lambda out: "rws" in out.split("\n")[0],
    ),
    # www-data given an interactive login shell.
    "www-data-shell": (
        "grep '^www-data:' /etc/passwd",
        lambda out: out.strip().endswith("/bin/bash"),
    ),
    # world-writable /etc/shadow.
    "bad-perms-userConfig": (
        "stat -c %a /etc/shadow",
        lambda out: out.strip() == "666",
    ),
}


# --------------------------------------------------------------------------- helpers

class CheckError(Exception):
    """A check could not run (missing data / unreachable infra) — clean message, no trace."""


def read_terraform_ctx():
    """Read the agent_context Terraform output (same shape as create-competition.py).

    Returns a dict with at least scoring_engine_ip / ssh_key_path / vm_username. ssh_key_path
    is resolved to an absolute path (Terraform's file() resolves it relative to terraform/).
    """
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
    # Nothing on disk matched; return the repo-root interpretation so the error is legible.
    return str((REPO_ROOT / val).resolve())


def build_ctx(args):
    """Assemble {scoring_engine_ip, ssh_key_path, vm_username}, honoring --engine-ip override."""
    ctx = {}
    tf_error = None
    if not args.engine_ip:
        ctx = read_terraform_ctx()  # only fatal path if we truly need the engine IP from TF
    else:
        try:
            ctx = read_terraform_ctx()
        except CheckError as e:
            tf_error = e  # fine — we have an override; fill the rest from .env
    if args.engine_ip:
        ctx["scoring_engine_ip"] = args.engine_ip
    if not ctx.get("scoring_engine_ip"):
        raise CheckError("no scoring_engine_ip (Terraform gave none and no --engine-ip).")
    # Prefer Terraform's resolved key/user; fall back to .env for override-only runs.
    if not ctx.get("ssh_key_path"):
        ctx["ssh_key_path"] = resolve_ssh_key()
    if not ctx.get("vm_username"):
        ctx["vm_username"] = os.environ.get("TF_VAR_vm_username")
    if tf_error:
        print(f"  (note: Terraform context unavailable, using .env: {tf_error})")
    return ctx


def ssh_via_gateway(ctx, target_ip, cmd, timeout=60):
    """SSH to a target box via the scoring-engine gateway (mirrors create-competition.py)."""
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    if not key or not scoring_user:
        raise CheckError("missing SSH key path or vm_username for gateway SSH.")
    proxy = (
        f"ssh -i {key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-W %h:%p {scoring_user}@{scoring_ip}"
    )
    return subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-o", f"ProxyCommand={proxy}",
            f"{BOX_USERNAME}@{target_ip}", cmd,
        ],
        capture_output=True, text=True, timeout=timeout,
    )


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
            # An admin line looks like:  admin  <password>
            if len(parts) >= 2 and parts[0] == "admin":
                return parts[1]
    return DEFAULT_ADMIN_PASSWORD


def count_local_injects(comp_dir):
    """Number of inject subdirectories (each carrying an inject.json). 0 if no injects/ dir."""
    injects_dir = comp_dir / "injects"
    if not injects_dir.is_dir():
        return None  # None == "competition ships no injects" (skip the check)
    return sum(1 for sub in injects_dir.iterdir() if sub.is_dir() and (sub / "inject.json").exists())


def load_boxes(comp_dir):
    """Load planted-misconfig boxes from nakon/config.json (repo-level, current deploy state)."""
    path = REPO_ROOT / "nakon" / "config.json"
    if not path.exists():
        raise CheckError(f"nakon/config.json not found: {path}")
    try:
        return json.loads(path.read_text()).get("machines", [])
    except json.JSONDecodeError as e:
        raise CheckError(f"nakon/config.json is not valid JSON: {e}")


# --------------------------------------------------------------------------- checks

def check_logins(base_url, teams, admin_password):
    """POST /api/login for admin + every team. Returns (all_ok, admin_session)."""
    print("\n[1/4] LOGIN")
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
        # Quotient authenticates teams by their team name (see credentials.txt / seed_and_start).
        if try_login(team_name, data.get("password", "")) is None:
            all_ok = False
    return all_ok, admin_session


def check_services(base_url, admin_session, teams, strict):
    """Report per-team service UP/DOWN from the latest scored round. Returns (query_ok, all_up)."""
    print("\n[2/4] SERVICES")
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
            all_up = False
            continue
        up = 0
        down_names = []
        for svc in services or []:
            any_service = True
            name = svc.get("ServiceName", "?")
            rounds = svc.get("Last10Rounds") or []
            checks = (rounds[0].get("Checks") if rounds else None) or []
            result = bool(checks[0].get("Result")) if checks else False
            if result:
                up += 1
            else:
                down_names.append(name)
        total = len(services or [])
        print(f"  {tname}: {up}/{total} services UP")
        for name in down_names:
            print(f"      DOWN: {name}")
        if down_names:
            all_up = False
    if not any_service:
        print("  (no services reported yet — engine may not have scored a round)")
    if strict and not all_up:
        print("  --strict-services: some services DOWN -> counts against exit code")
    return True, all_up


def check_misconfig(ctx, boxes):
    """SSH via gateway to one box and confirm >=1 planted misconfig. Returns bool."""
    print("\n[3/4] MISCONFIG SPOT-CHECK")
    # Pick the first box that has at least one config we know how to verify.
    target = None
    for box in boxes:
        verifiable = [c for c in box.get("configurations", []) if c in MISCONFIG_CHECKS]
        if box.get("ip") and verifiable:
            target = (box, verifiable)
            break
    if target is None:
        print("  FAIL  — no box in nakon/config.json carries a verifiable misconfig.")
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


def check_injects(base_url, admin_session, comp_dir):
    """Compare engine inject count to the competition's injects/ dir. Returns (relevant, ok)."""
    print("\n[4/4] INJECTS")
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
    ok = len(injects) >= expected
    print(f"  {'PASS' if ok else 'FAIL'}  engine has {len(injects)} inject(s), "
          f"expected {expected} from injects/ dir")
    return True, ok


# --------------------------------------------------------------------------- main

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
        ctx = build_ctx(args)
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
    services_query_ok, services_all_up = check_services(base_url, admin_session, teams,
                                                         args.strict_services)
    misconfig_ok = check_misconfig(ctx, boxes)
    injects_relevant, injects_ok = check_injects(base_url, admin_session, comp_dir)

    # Exit-code gate: logins + misconfig + injects (if any). Services are informational,
    # unless --strict-services promotes them.
    gate = {
        "logins": logins_ok,
        "misconfig": misconfig_ok,
        "injects": injects_ok,
    }
    if args.strict_services:
        gate["services(strict)"] = services_query_ok and services_all_up

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  logins           : {'PASS' if logins_ok else 'FAIL'}")
    svc_note = "informational"
    if args.strict_services:
        svc_note = "PASS" if (services_query_ok and services_all_up) else "FAIL"
    print(f"  services         : {'UP' if services_all_up else 'some DOWN'} "
          f"({'query ok' if services_query_ok else 'query failed'}) [{svc_note}]")
    print(f"  misconfig        : {'PASS' if misconfig_ok else 'FAIL'}")
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
