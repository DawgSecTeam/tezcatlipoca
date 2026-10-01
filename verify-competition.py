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

from range_ops import guest_agent_exec_root, guest_agent_exec_windows, vm_id_for
from quotient.setup import expected_service_names
from ssh_ops import engine_ssh_opts, gateway_proxy
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


def read_terraform_ctx(comp_dir=None):
    """Read agent_context from `terraform output -json`; resolve ssh_key_path absolute.

    Prefer the competition's own per-comp state (competitions/<id>/terraform); fall
    back to the legacy shared terraform/ dir when the per-comp dir has no state."""
    tf_dir = REPO_ROOT / "terraform"
    if comp_dir is not None:
        per_comp = Path(comp_dir) / "terraform"
        if (per_comp / "terraform.tfstate").exists():
            tf_dir = per_comp
    try:
        raw = subprocess.run(
            ["terraform", "output", "-json"],
            cwd=str(tf_dir),
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
        ctx["ssh_key_path"] = str((tf_dir / key_path).resolve())
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
        ctx = read_terraform_ctx(comp_dir)
    else:
        try:
            ctx = read_terraform_ctx(comp_dir)
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
    scoring_user = ctx["vm_username"]
    box_username = ctx.get("box_username", BOX_USERNAME_DEFAULT)
    if not key or not scoring_user:
        raise CheckError("missing SSH key path or vm_username for gateway SSH.")
    try:
        return subprocess.run(
            [
                "ssh", "-i", key,
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=10",
                "-o", f"ProxyCommand={gateway_proxy(ctx)}",
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
            ["ssh", "-i", key, *engine_ssh_opts(ctx),
             f"{scoring_user}@{scoring_ip}", cmd],
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


def check_services(base_url, admin_session, teams, strict, expected_names=frozenset()):
    """Report per-team service UP/DOWN from the latest scored round.

    Returns (query_ok, all_up, pins_registered). pins_registered gates the scoreboard's
    actual ServiceName set against the pins' expected set — a pin that never registered
    (the regression-4x1 same-TYPE collapse: 12 pins, 11 checks) scores nothing and
    silent-tallies as UP-absent, so it fails the exit code regardless of --strict."""
    print("\n[2/5] SERVICES")
    if admin_session is None:
        print("  SKIP  — no admin session (login failed).")
        return False, False, True
    try:
        r = admin_session.get(f"{base_url}/api/teams", timeout=10)
        r.raise_for_status()
        api_teams = r.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"  FAIL  — could not fetch /api/teams: {e}")
        return False, False

    query_ok = True
    all_up = True
    actual_names = set()
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
        actual_names |= {svc.get("ServiceName", "?") for svc in services or []}
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
    pins_ok = True
    if expected_names:
        missing = sorted(expected_names - actual_names)
        extras = sorted(actual_names - expected_names)
        if missing:
            print(f"  FAIL  pin(s) never registered on the scoreboard: {', '.join(missing)}")
            print("        (a pin whose check never registered scores nothing — the "
                  "regression-4x1 same-TYPE collapse shape)")
            pins_ok = False
        else:
            print(f"  EXPECTED-PINS  all {len(expected_names)} pinned checks registered")
        if extras:
            print(f"  WARN  scoreboard services not derived from box_services.json: {', '.join(extras)}")
    return query_ok, all_up, pins_ok


def check_isolation(ctx, teams, boxes):
    """Confirm isolation DROP rule present and actually blocks cross-team traffic.

    Returns True (verified), False (broken), or None (couldn't run the live
    probe — reported as SKIP, never as a pass): a rule that exists but couldn't
    be exercised must not read as verified isolation."""

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
    box_os = {}
    for box in boxes:
        parts = box.get("ip", "").split(".")
        if len(parts) != 4 or parts[2] not in identifiers:
            continue
        # Prefer a Linux box per team: the probe authenticates as the linux
        # box_username and Windows boxes want Administrator (a medic probe there
        # always 255s — live-found 2026-09-29 on the dc01-first lineup).
        is_linux = "win" not in str(box.get("os", "")).lower()
        cur = team_ips.get(parts[2])
        if cur is None or (is_linux and "win" in str(box_os.get(cur, "")).lower()):
            team_ips[parts[2]] = box["ip"]
        box_os[box["ip"]] = str(box.get("os", ""))
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
        print(f"  SKIP  — cross-team connection test couldn't run ({e}); rule-presence "
              "check above passed, but an untested rule is NOT a verified pass.")
        return None

    if proc.returncode != 0:
        print(f"  SKIP  — couldn't SSH to {from_ip} to run the test "
              f"(rc={proc.returncode}): {(proc.stderr or '').strip()[:150]}; rule-presence "
              "check above passed, but an untested rule is NOT a verified pass.")
        return None

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


def _target_vmid(comp_dir, box):
    """VMID for a nakon machine entry: frozen targets.json, falling back to boxes.json order."""
    try:
        identifier = box["ip"].split(".")[2]
        base_name = box["name"].rsplit("-team", 1)[0]
        targets_path = Path(comp_dir) / "targets.json"
        if targets_path.exists():
            for t in json.loads(targets_path.read_text()).get("targets", {}).values():
                if str(t["identifier"]) == identifier and t["box_name"] == base_name:
                    return t["vmid"]
        boxes_json = json.loads((Path(comp_dir) / "boxes.json").read_text())
        idx = next(i for i, b in enumerate(boxes_json) if b.get("name") == base_name)
        return vm_id_for(identifier, idx)
    except Exception:
        return None


def misconfig_via_guest_agent(comp_dir, box, verifiable):
    """SSH-dead fallback: run the same checks via the QEMU guest agent.

    scrim-extreme-cyberfield-2026-09-22 lost its only verifier FAIL to dead
    Linux SSH while the planted artifacts were independently confirmed fine —
    the agent (virtio-serial, no network) reaches the box anyway."""
    vmid = _target_vmid(comp_dir, box)
    if vmid is None:
        print("  (guest-agent fallback unavailable: could not resolve the box's vmid)")
        return False
    node = os.environ.get("TF_VAR_proxmox_node")
    if not node:
        print("  (guest-agent fallback unavailable: TF_VAR_proxmox_node not set)")
        return False
    print(f"  SSH unavailable — falling back to guest-agent exec on vmid {vmid}...")
    for config in verifiable:
        cmd, predicate = MISCONFIG_CHECKS[config]
        try:
            rc, out, err = guest_agent_exec_root(node, vmid, cmd)
        except Exception as e:
            print(f"  WARN  {config}: guest-agent exec failed ({e})")
            continue
        if rc != 0:
            print(f"  ....  '{config}': guest rc={rc}: {(err or '').strip()[:80]}")
            continue
        if predicate(out):
            print(f"  PASS  confirmed '{config}' via guest agent — `{cmd}` -> {out.strip()[:80]}")
            return True
        print(f"  ....  '{config}' not present: {out.strip()[:80]}")
    return False


def check_misconfig(ctx, boxes, comp_dir):
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
    ssh_dead = False
    for config in verifiable:
        cmd, predicate = MISCONFIG_CHECKS[config]
        try:
            proc = ssh_via_gateway(ctx, ip, cmd)
        except subprocess.TimeoutExpired:
            print(f"  WARN  {config}: SSH timed out.")
            ssh_dead = True
            continue
        except CheckError as e:
            print(f"  WARN  SSH to the box is unavailable ({e})")
            ssh_dead = True
            break
        if proc.returncode != 0:
            print(f"  WARN  {config}: command failed (rc={proc.returncode}): "
                  f"{(proc.stderr or '').strip()[:120]}")
            continue
        out = proc.stdout
        if predicate(out):
            print(f"  PASS  confirmed '{config}' — `{cmd}` -> {out.strip()[:80]}")
            return True
        print(f"  ....  '{config}' not present: {out.strip()[:80]}")
    if ssh_dead:
        if misconfig_via_guest_agent(comp_dir, box, verifiable):
            return True
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
    closed = closed_injects(injects)
    if closed:
        print(f"  WARN  {len(closed)} inject(s) already CLOSED — submissions return 'Inject is closed'. "
              "Offsets are anchored at the phase-7 deploy time, so a reset/rerun long after "
              f"deploy finds them expired: {', '.join(t for t, _ in closed)}")
    return True, ok


_INJECT_CLOSE_KEYS = ("CloseTime", "close_time", "CloseAt", "close_at", "Close", "close")


def check_round_loop(base_url, admin_session, fix=False):
    """Scoring round loop vs an engine reboot. The Docker containers restart after
    a reboot but the round loop stays stopped (pfsense-ad: frozen scoreboard) —
    and verify reads the LAST SCORED round, reporting stale DOWN as if live.
    /api/engine is snake_case (live-confirmed 2026-09-29): `running` (False =
    paused), `competition_started`, `current_round_time` (RFC3339; the Go zero
    time "0001-01-01T00:00:00Z" means the loop is NOT cycling), and
    `last_round.StartTime`. Informational: WARN with the exact remediation;
    --fix-round-loop runs the two POSTs."""
    print("\n  (scoring round loop — stops silently after an engine reboot)")
    if admin_session is None:
        print("  SKIP  — no admin session.")
        return True
    try:
        r = admin_session.get(f"{base_url}/api/engine", timeout=10)
        r.raise_for_status()
        eng = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  WARN  — could not read /api/engine: {e}")
        return True
    if not isinstance(eng, dict):
        return True
    if eng.get("running") is False:
        print("  PASS  — engine paused (round loop not expected to advance)")
        return True

    from datetime import datetime, timezone

    def _rfc3339(value):
        if not isinstance(value, str) or not value:
            return None
        try:
            when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when

    cur_when = _rfc3339(eng.get("current_round_time"))
    if cur_when is not None and cur_when.year <= 1:
        cur_when = None  # the Go zero time — the loop is not cycling
    started = _rfc3339((eng.get("last_round") or {}).get("StartTime"))
    if started is None or cur_when is not None:
        print("  PASS  — round loop advancing")
        return True
    age_min = (datetime.now(timezone.utc) - started).total_seconds() / 60
    if age_min < 10:
        print("  PASS  — round loop starting (first round pending within Delay)")
        return True
    print(f"  WARN  — round loop looks STOPPED: last round started {age_min:.0f} min ago "
          "and current_round_time is the zero time (engine rebooted? the loop does not "
          "self-resume)")
    print('         fix: POST /api/competition/start {"started":true} then '
          'POST /api/engine/pause {"pause":false} — a fresh round lands within Delay s')
    if fix:
        r1 = admin_session.post(f"{base_url}/api/competition/start",
                                json={"started": True}, timeout=10)
        r2 = admin_session.post(f"{base_url}/api/engine/pause",
                                json={"pause": False}, timeout=10)
        print(f"  --fix-round-loop: start={r1.status_code} unpause={r2.status_code} — "
              f"re-run verify to confirm a fresh round landed")
    return True


def closed_injects(injects, now=None):
    """[(title, close_time)] for injects whose close time is already past. Tolerant of the
    engine's JSON key casing; injects with no parseable close time are ignored."""
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    out = []
    for inj in injects:
        raw = next((inj[k] for k in _INJECT_CLOSE_KEYS if inj.get(k)), None)
        if not isinstance(raw, str):
            continue
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < now:
            out.append((inj.get("Title") or inj.get("title") or "?", raw))
    return out



_WIN_DOMAIN_PS = (
    "$cs = Get-WmiObject Win32_ComputerSystem; "
    "'ROLE=' + $cs.DomainRole; 'DOMAIN=' + $cs.Domain; 'PARTOF=' + $cs.PartOfDomain; "
    "'MSID=' + (New-Object System.Security.Principal.NTAccount('Administrator'))"
    ".Translate([System.Security.Principal.SecurityIdentifier]).AccountDomainSid.Value; "
    "if ($cs.DomainRole -ge 4) { try { $d = Get-ADDomain -ErrorAction Stop; "
    "'DSID=' + $d.DomainSID.Value; 'DNSROOT=' + $d.DNSRoot; "
    "'SVC=' + [bool](Get-ADUser -Filter \"SamAccountName -eq 'svc-support'\" -ErrorAction Stop) "
    "} catch { 'ADERR=' + $_.Exception.Message } }"
)


def _kv(out):
    return dict(l.split("=", 1) for l in (out or "").splitlines() if "=" in l)


def _valid_domain_sid(sid):
    """S-1-5-21-<a>-<b>-<c> — a DOMAIN SID has exactly three sub-authorities and no
    trailing RID (an 8-part value is an account SID, not a domain SID)."""
    parts = (sid or "").split("-")
    return (len(parts) == 7 and sid.startswith("S-1-5-21-")
            and all(p.isdigit() for p in parts[3:]))


def check_domains(comp_dir, teams, boxes, ctx=None):
    """Domain gate (replaces the freeze's operator attestation with a live check).

    Per team: the DC answers Get-ADDomain for team<id>.local with a syntactically
    valid DomainSID, the planted AD misconfig (svc-support) exists, and every member
    box is actually joined. Across teams: DomainSIDs must be unique (a collision
    means DC promotion reused image state — winad-testrun 2026-09-25 found exactly
    that); with a single team uniqueness cannot be exercised, so the PASS says so
    instead of claiming it. Member machine SIDs are reported, not gated: members
    linked-cloned from one golden share them by design, which is harmless for
    isolated forests. A present-but-malformed domain_roles.json fails closed.
    Returns None when the lineup has no domain_roles.json."""
    roles_path = comp_dir / "domain_roles.json"
    if not roles_path.exists():
        print("  SKIP  — no domain_roles.json")
        return None
    try:
        roles = json.loads(roles_path.read_text())
    except (OSError, ValueError) as e:
        print(f"  FAIL  domain_roles.json is unreadable/malformed ({str(e)[:80]})")
        return False
    if not isinstance(roles, dict) or not all(
            isinstance(name, str) and isinstance(role, str)
            for name, role in roles.items()):
        print("  FAIL  domain_roles.json must map box names to 'dc' or 'member' strings")
        return False
    bad = {name: role for name, role in roles.items() if role not in ("dc", "member")}
    if bad:
        print("  FAIL  domain_roles.json has invalid role value(s): "
              + ", ".join(f"{name}={role!r}" for name, role in sorted(bad.items()))
              + " (expected 'dc' or 'member')")
        return False
    node = os.environ.get("TF_VAR_proxmox_node")
    # Box TYPES (boxes.json order = vmid order), not verify's per-team nakon machines.
    try:
        boxes = json.loads((comp_dir / "boxes.json").read_text())
    except (OSError, ValueError) as e:
        print(f"  FAIL  boxes.json is unreadable/malformed ({str(e)[:80]})")
        return False
    idx = {b["name"]: i for i, b in enumerate(boxes)}
    unknown = [name for name in roles if name not in idx]
    if unknown:
        print("  FAIL  domain_roles.json names box(es) absent from boxes.json: "
              + ", ".join(sorted(unknown)))
        return False
    if not teams:
        print("  FAIL  no teams loaded — nothing to check domain roles against")
        return False
    dc_name = next((n for n, r in roles.items() if r == "dc"), None)
    ok = True
    domain_sids, machine_sids = {}, {}
    for team_key, team in sorted(teams.items()):
        ident = team["identifier"]
        domain = f"team{ident}.local"
        for name, role in roles.items():
            vmid = vm_id_for(ident, idx[name])
            windows = "win" in (boxes[idx[name]].get("template") or "").lower()
            try:
                if windows:
                    rc, out, err = guest_agent_exec_windows(node, vmid, _WIN_DOMAIN_PS, timeout=120)
                    kv = _kv(out)
                else:
                    _realm_cmd = (f"realm list 2>/dev/null | grep -qi 'domain-name: *{domain}' "
                                  f"&& echo JOINED=1 || echo JOINED=0")
                    try:
                        rc, out, err = guest_agent_exec_root(node, vmid, _realm_cmd, timeout=60)
                        kv = _kv(out)
                    except Exception as agent_err:
                        # The PVE agent channel has a per-instance exec breaker that can
                        # stay tripped (amongus-cde 2026-09-30: airship's failed join
                        # probes tripped it permanently). Linux boxes are still reachable
                        # over gateway SSH — fall back to it before failing the gate.
                        try:
                            box_ip = f"192.168.{ident}.{boxes[idx[name]]['last_octet']}"
                            proc = ssh_via_gateway(ctx or {"ssh_key_path": str(resolve_ssh_key())},
                                                   box_ip, _realm_cmd, timeout=60)
                            kv = _kv(proc.stdout)
                            if kv.get("JOINED") is None:
                                raise CheckError(f"unparseable realm probe: {proc.stdout[:80]}")
                            print(f"  INFO  {team_key}/{name}: agent channel unavailable, "
                                  f"probed over gateway SSH")
                        except Exception as ssh_err:
                            print(f"  FAIL  {team_key}/{name}: guest-agent probe failed "
                                  f"({str(agent_err)[:80]}) and gateway-SSH fallback failed "
                                  f"({str(ssh_err)[:60]})")
                            ok = False
                            continue
            except Exception as e:
                print(f"  FAIL  {team_key}/{name}: guest-agent probe failed ({str(e)[:80]})")
                ok = False
                continue
            if role == "dc":
                if kv.get("ADERR") or kv.get("DNSROOT", "").lower() != domain:
                    print(f"  FAIL  {team_key}/{name}: DC not serving {domain} "
                          f"({kv.get('ADERR') or kv.get('DNSROOT') or (err or '').strip()[:80]})")
                    ok = False
                    continue
                dsid = kv.get("DSID") or ""
                if not _valid_domain_sid(dsid):
                    print(f"  FAIL  {team_key}/{name}: {domain} reports no valid "
                          f"DomainSID ({dsid or 'DSID missing'} — a promoted DC must "
                          f"answer Get-ADDomain with an S-1-5-21-* SID)")
                    ok = False
                    continue
                domain_sids.setdefault(dsid, []).append(team_key)
                svc = kv.get("SVC", "").lower() == "true"
                print(f"  {'PASS' if svc else 'FAIL'}  {team_key}/{name}: {domain} "
                      f"DomainSID {dsid}; svc-support {'present' if svc else 'MISSING'}")
                ok &= svc
            elif windows:
                joined = kv.get("PARTOF", "").lower() == "true" and kv.get("DOMAIN", "").lower() == domain
                machine_sids.setdefault(kv.get("MSID"), []).append(f"{team_key}/{name}")
                print(f"  {'PASS' if joined else 'FAIL'}  {team_key}/{name}: "
                      f"{'joined' if joined else 'NOT joined'} to {domain}")
                ok &= joined
            else:
                joined = kv.get("JOINED") == "1"
                print(f"  {'PASS' if joined else 'FAIL'}  {team_key}/{name}: "
                      f"{'realm-joined' if joined else 'NOT realm-joined'} to {domain}")
                ok &= joined
    dupes = {sid: t for sid, t in domain_sids.items() if len(t) > 1}
    if dupes:
        for sid, t in dupes.items():
            print(f"  FAIL  DomainSID {sid} shared by {', '.join(t)} — DC promotion reused "
                  f"image state (the DC box type must use an unbooted golden)")
        ok = False
    elif domain_sids and dc_name:
        if len(teams) > 1:
            print(f"  PASS  {len(domain_sids)} team domain(s), all DomainSIDs unique")
        else:
            print(f"  PASS  1 team domain, DomainSID well-formed "
                  f"(uniqueness needs a second team to exercise)")
    shared = {sid: v for sid, v in machine_sids.items() if len(v) > 1}
    for sid, v in shared.items():
        print(f"  INFO  member machine SID {sid} shared by {', '.join(v)} "
              f"(linked clones of one golden — harmless for isolated forests)")
    return ok


def check_plant_coverage(comp_dir):
    """M4 plant-coverage gate: every machine's FULL expected configuration list
    (nakon-config.json) must have actually planted.

    Deploy records failures per machine in .deploy_state.json["plant_coverage_failed"]
    (machine -> [config names whose nakon step reported rc != 0], or every config when
    a machine died before reporting any step). Golden-stage entries map onto every team
    copy of that box (a golden failure means the clones inherited the gap). This is the
    backstop that catches a broken/undeclared-var config the moment it fails to plant,
    instead of a mid-competition discovery."""
    state_path = comp_dir / ".deploy_state.json"
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {}
    failed = state.get("plant_coverage_failed") or {}
    config_path = comp_dir / "nakon-config.json"
    if not config_path.exists():
        print("  SKIP  — no nakon-config.json (nothing expected).")
        return True, None
    try:
        machines = json.loads(config_path.read_text())["machines"]
    except (OSError, ValueError, KeyError) as e:
        print(f"  FAIL  — cannot read nakon-config.json: {e}")
        return True, False

    def cfg_name(c):
        return c if isinstance(c, str) else c["name"]

    unplanted = {}
    for m in machines:
        expected = {cfg_name(c) for c in m["configurations"]}
        bad = set(failed.get(m["name"]) or [])
        golden_key = f"{m['name'].rsplit('-team', 1)[0]}-golden"
        bad |= {f"{c} (golden-stage)" for c in (failed.get(golden_key) or [])}
        missing = sorted(bad & expected | {c for c in bad if c.startswith("<machine")})
        if bad:
            unplanted[m["name"]] = sorted(bad)
    if unplanted:
        for name, cfgs in sorted(unplanted.items()):
            print(f"  FAIL  {name}: not planted: {', '.join(cfgs)}")
        return True, False
    print(f"  PASS  all {len(machines)} machine(s) report full config coverage")
    return True, True


def freeze_hashes(comp_dir):
    """The template hashes a freeze would record (None when nothing is recorded yet)."""
    path = comp_dir / ".template-hashes.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def do_freeze(comp_dir, args, gate, coverage_ok):
    """M4 freeze: record the template hashes this verify just passed against, plus the
    code commit, timestamp, and gate results. Preconditions: every gate PASS including
    plant-coverage and services; Windows/domain lineups additionally require the
    operator's --windows-domain-validated attestation (that the run exercised them)."""
    from template_ops import git_commit_info
    import time as _time

    hashes = freeze_hashes(comp_dir)
    if not hashes or not (hashes.get("engine") or {}).get("hash") or not hashes.get("golden"):
        print("  FREEZE refused — no template hash record (.template-hashes.json); "
              "deploy once on the M4 pipeline first.")
        return False
    if not coverage_ok:
        print("  FREEZE refused — plant-coverage did not pass.")
        return False
    if not all(gate.values()):
        print(f"  FREEZE refused — failing gates: "
              f"{', '.join(k for k, v in gate.items() if not v)}")
        return False
    boxes = load_boxes(comp_dir)
    roles = comp_dir / "domain_roles.json"
    needs_domain = (roles.exists()
                    or any("win" in (b.get("template") or "").lower() for b in (boxes or [])))
    if needs_domain and "domains" not in gate and not args.windows_domain_validated:
        print("  FREEZE refused — this lineup uses Windows/domains; pass "
              "--windows-domain-validated to attest that this run's Windows/domain "
              "validation (DomainSIDs, machine SIDs, three-pass ordering) passed.")
        return False
    record = {
        "frozen_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "code": git_commit_info(),
        "hashes": {"engine": hashes["engine"], "golden": hashes["golden"]},
        "verify_report": {"gates": gate, "plant_coverage": coverage_ok},
        "windows_domain_validated": bool(args.windows_domain_validated),
    }
    path = comp_dir / ".frozen.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=2))
    import os as _os
    _os.replace(tmp, path)
    _os.chmod(path, 0o600)
    print(f"  FROZEN — {path} written. Engine + {len(hashes['golden'])} golden hash(es) "
          f"recorded. Rebuild is now refused on config drift; --full teardown needs "
          f"--end-of-competition.")
    return True


def do_unfreeze(comp_dir, confirm):
    if not confirm:
        print("  UNFREEZE refused — pass --confirm-unfreeze. Unfreezing mid-event "
              "defeats the freeze; it is meant for use BEFORE the competition starts.")
        return False
    path = comp_dir / ".frozen.json"
    if not path.exists():
        print("  Nothing to unfreeze.")
        return True
    path.unlink()
    print("  UNFROZEN — .frozen.json removed.")
    return True



def main():
    parser = argparse.ArgumentParser(description="Verify a deployed Quotient competition range.")
    parser.add_argument("comp_dir", help="path to competitions/<id>")
    parser.add_argument("--engine-ip", help="override the scoring-engine IP (skip Terraform)")
    parser.add_argument("--admin-password", help="override the Quotient admin password")
    parser.add_argument("--strict-services", action="store_true",
                        help="also require every service UP for a passing exit code")
    parser.add_argument("--fix-round-loop", action="store_true", dest="fix_round_loop",
                        help="when the scoring round loop looks stopped after an engine "
                             "reboot, run the start/unpause POSTs instead of only warning")
    parser.add_argument("--expect-no-vulns", action="store_true", dest="expect_no_vulns",
                        help="validation comps that deliberately plant zero misconfigurations "
                             "(box_vulns.json all-empty): skip the misconfig gates instead of "
                             "failing on them")
    parser.add_argument("--freeze", action="store_true",
                        help="M4: after a PASSING verify (all gates + plant-coverage), "
                             "record the template hashes + code commit in .frozen.json. "
                             "Windows/domain lineups also need --windows-domain-validated.")
    parser.add_argument("--windows-domain-validated", action="store_true", dest="windows_domain_validated",
                        help="operator attestation that this run exercised the Windows/"
                             "domain validation (DomainSIDs unique per team, machine SIDs "
                             "assessed, three-pass ordering held) — required to freeze "
                             "such lineups.")
    parser.add_argument("--unfreeze", action="store_true",
                        help="M4: remove .frozen.json (needs --confirm-unfreeze; for use "
                             "BEFORE the competition starts).")
    parser.add_argument("--confirm-unfreeze", action="store_true", dest="confirm_unfreeze")
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

    if args.unfreeze:
        return 0 if do_unfreeze(comp_dir, args.confirm_unfreeze) else 1

    logins_ok, admin_session = check_logins(base_url, teams, admin_password)
    print("\n  (default-credential regression guard)")
    no_default_creds_ok = check_no_default_creds(comp_dir)
    # boxes.json (box TYPES, keyed by name in box_services.json) — nakon-config
    # machines carry team-suffixed names the pin map doesn't use
    try:
        box_list = json.loads((comp_dir / "boxes.json").read_text())
        pinned_services = json.loads((comp_dir / "box_services.json").read_text())
    except (OSError, ValueError):
        box_list, pinned_services = [], {}
    expected_names = expected_service_names(pinned_services, box_list) if pinned_services else set()
    services_query_ok, services_all_up, pins_registered = check_services(
        base_url, admin_session, teams, args.strict_services, expected_names)
    isolation_ok = check_isolation(ctx, teams, boxes)
    print("\n  (live-ops health check status — informational)")
    report_healthcheck_status(ctx)
    if args.expect_no_vulns:
        print("\n[4/5] MISCONFIG SPOT-CHECK")
        print("  SKIP  — --expect-no-vulns: this comp deliberately plants no misconfigurations")
        misconfig_ok = True
        misconfig_survival_ok = True
    else:
        misconfig_ok = check_misconfig(ctx, boxes, comp_dir)
        misconfig_survival_ok = check_misconfig_survival(ctx, boxes)
    report_beacons(ctx, boxes)
    injects_relevant, injects_ok = check_injects(base_url, admin_session, comp_dir)
    check_round_loop(base_url, admin_session, fix=args.fix_round_loop)
    print("\n  (M4 plant coverage — expected vs. actually planted, per machine)")
    coverage_checked, coverage_ok = check_plant_coverage(comp_dir)
    print("\n  (AD domains — promotion, joins, AD plants, DomainSID uniqueness)")
    domains_ok = check_domains(comp_dir, teams, boxes, ctx=ctx)

    gate = {
        "logins": logins_ok,
        "no_default_creds": no_default_creds_ok,
        "isolation": isolation_ok is True,
        "misconfig": misconfig_ok,
        "misconfig_survival": misconfig_survival_ok,
        "injects": injects_ok,
    }
    if args.strict_services:
        gate["services(strict)"] = services_query_ok and services_all_up
    if expected_names:
        gate["pins_registered"] = pins_registered
    if coverage_checked and coverage_ok is not None:
        gate["plant_coverage"] = coverage_ok
    if domains_ok is not None:
        gate["domains"] = domains_ok

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
    if expected_names:
        print(f"  pins_registered  : "
              f"{'PASS' if pins_registered else 'FAIL'} ({len(expected_names)} pinned checks)")
    isolation_note = ("PASS" if isolation_ok is True
                      else "SKIP — live probe couldn't run, unverified" if isolation_ok is None
                      else "FAIL")
    print(f"  isolation        : {isolation_note}")
    misconfig_note = "PASS"
    if args.expect_no_vulns:
        misconfig_note = "SKIP (--expect-no-vulns)"
    print(f"  misconfig        : {misconfig_note}")
    print(f"  misconfig_surviv.: {misconfig_note}")
    print(f"  injects          : {'PASS' if injects_ok else 'FAIL'}"
          f"{'' if injects_relevant else ' (none — skipped)'}")
    tally = None
    state_path = comp_dir / ".deploy_state.json"
    if state_path.exists():
        try:
            tally = json.loads(state_path.read_text()).get("nakon_failed_steps")
        except (OSError, ValueError):
            tally = None
    if tally is None:
        print("  plant integrity  : no nakon FAILED tally in .deploy_state.json "
              "(pre-tally deploy)")
    elif tally:
        print(f"  plant integrity  : WARNING — last nakon plant recorded {len(tally)} "
              f"FAILED step(s): {', '.join(s[:60] for s in tally[:3])}"
              f"{' …' if len(tally) > 3 else ''}")
    else:
        print("  plant integrity  : last nakon plant recorded 0 FAILED steps")
    if domains_ok is not None:
        print(f"  domains          : {'PASS' if domains_ok else 'FAIL'}")
    if coverage_checked and coverage_ok is not None:
        print(f"  plant coverage   : {'PASS' if coverage_ok else 'FAIL'}"
              + ("" if coverage_ok else " — unplanted configs above"))
    hashes = freeze_hashes(comp_dir)
    if hashes and (hashes.get("engine") or {}).get("hash"):
        golden_short = {k: v["hash"][:12] for k, v in (hashes.get("golden") or {}).items()}
        print(f"  templates        : engine {hashes['engine']['hash'][:12]} | "
              f"golden {json.dumps(golden_short)}")
    frozen = (comp_dir / ".frozen.json").exists()
    if frozen:
        try:
            frozen_at = json.loads((comp_dir / ".frozen.json").read_text()).get("frozen_at")
        except (OSError, ValueError):
            frozen_at = "?"
        print(f"  freeze           : FROZEN since {frozen_at}")

    passed = all(gate.values())
    print("\n" + ("RESULT: PASS — competition looks healthy."
                  if passed else "RESULT: FAIL — see failing checks above."))
    if args.freeze:
        ok = do_freeze(comp_dir, args, gate, coverage_ok if coverage_checked else None)
        return 0 if (passed and ok) else 1
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
