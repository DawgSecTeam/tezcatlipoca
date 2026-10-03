#!/usr/bin/env python3
"""Post-deploy verifier for a Quotient scoring range: logins, services, isolation, misconfig spot-check, injects."""

import argparse
import dataclasses
import enum
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
    import urllib3
except ImportError as e:
    print(f"ERROR: missing dependency ({e}). Need: requests, python-dotenv.", file=sys.stderr)
    sys.exit(2)

from dotenv import load_dotenv

from domain_ops import team_domain
import round_loop
from range_ops import guest_agent_exec_root, guest_agent_exec_windows, vm_id_for
from quotient.setup import expected_service_names
from ssh_ops import engine_ssh_opts, gateway_proxy
from utils import (BOX_USERNAME_DEFAULT, MAX_CONCURRENCY, PRINT_LOCK, load_users_config,
                   run_concurrent)

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


class Status(enum.Enum):
    """Tri-state gate verdict.

    SKIP_UNAVAILABLE means the gate could not be evaluated (dead SSH, missing
    state, no vantage point) and is deliberately NOT a pass: a check that never
    ran exiting 0 is how a dead box reads as a healthy range (live-found
    2026-10-02: isolation's cross-team probe "PASSed" on stopped VMs). The
    operator can waive a specific gate with --allow-unverified."""
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP_UNAVAILABLE = "SKIP"


@dataclasses.dataclass
class GateResult:
    """One gate's verdict — the SUMMARY and the exit code both read THIS object,
    so the printed word and the gate dict can never drift apart (the old
    hand-printed SUMMARY said "SKIP — unverified" for isolation while the dict
    recorded a FAIL)."""
    name: str
    status: Status
    detail: str = ""
    gating: bool = True      # False = reported in SUMMARY, excluded from the verdict
    label: str = ""          # SUMMARY label; defaults to name

    def __post_init__(self):
        if not self.label:
            self.label = self.name

    @property
    def passed(self):
        return self.status is Status.PASS


def _pass(name, detail="", gating=True, label=""):
    return GateResult(name, Status.PASS, detail, gating, label)


def _fail(name, detail="", gating=True, label=""):
    return GateResult(name, Status.FAIL, detail, gating, label)


def _skip(name, detail="", gating=True, label=""):
    return GateResult(name, Status.SKIP_UNAVAILABLE, detail, gating, label)


def _bool_gate(name, ok, detail="", gating=True, label=""):
    """Wrap a plain bool check (no SKIP outcome) as a GateResult."""
    return GateResult(name, Status.PASS if ok else Status.FAIL, detail, gating, label)


class RunBudget:
    """Optional whole-run wall-clock budget for --timeout (default: disabled).

    Additive, not a re-gate: with --timeout absent every query says `expired()` is
    False, so the run is byte-for-byte what it was before. Deliberately cooperative —
    it is checked BETWEEN gates, never mid-flight, so it can never kill an ssh or
    interrupt a Proxmox task half-done (a half-deleted snapshot or half-converted
    golden is worse than a slow verify); worst-case overshoot is the one gate already
    running. A gate that never ran is recorded SKIP_UNAVAILABLE — deliberately
    non-passing — so a budget can bound a verify but can never turn an unevaluated
    range into a PASS; --allow-unverified is the explicit waiver.
    """

    def __init__(self, seconds):
        self.seconds = int(seconds or 0)
        self.deadline = time.monotonic() + self.seconds if self.seconds > 0 else None
        self.label = f"--timeout {self.seconds}s"

    def expired(self):
        return self.deadline is not None and time.monotonic() >= self.deadline


# Round cadence: quotient/setup.py build_event_conf's MiscSettings.Delay. Used as
# the freshness unit — a scored round older than _FRESHNESS_ROUNDS × Delay means
# the scoreboard is frozen and an old UP is not a live UP (see D7 / the
# pfsense-ad frozen-scoreboard incident).
_ROUND_DELAY_SECONDS = 60
_FRESHNESS_ROUNDS = 5

_GATE_LABEL_WIDTH = 18


def _rfc3339(value):
    """Parse an RFC3339 timestamp; None when absent/unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when


# Quotient's Last10Rounds key casing is not guaranteed; tolerate the variants the
# codebase already sees elsewhere (check_round_loop reads the capital form).
_ROUND_START_KEYS = ("StartTime", "start_time", "startTime")


def _round_start(round_entry):
    """StartTime of one Last10Rounds entry, however it's cased."""
    if not isinstance(round_entry, dict):
        return None
    raw = next((round_entry[k] for k in _ROUND_START_KEYS if round_entry.get(k)), None)
    return _rfc3339(raw)


def gate_verdict(results, allow_unverified=()):
    """(gate, passed) derived from GateResults.

    gate maps every gating gate name to `status is PASS` (what do_freeze records).
    A SKIP is non-passing — it must not yield exit 0 — unless the operator named
    that gate in --allow-unverified; a FAIL always fails."""
    allowed = set(allow_unverified)

    def _allowed(r):
        return r.name in allowed or r.label in allowed

    gate = {r.name: r.passed for r in results if r.gating}
    passed = all(r.status is Status.PASS
                 or (r.status is Status.SKIP_UNAVAILABLE and _allowed(r))
                 for r in results if r.gating)
    return gate, passed


def summary_lines(results, allow_unverified=()):
    """The SUMMARY body, generated from the GateResults themselves (D6)."""
    allowed = set(allow_unverified)
    lines = []
    for r in results:
        line = f"  {r.label:<{_GATE_LABEL_WIDTH}}: {r.status.value}"
        if r.detail:
            line += f"  {r.detail}"
        if not r.gating:
            line += " [informational]"
        elif r.status is Status.SKIP_UNAVAILABLE and (r.name in allowed or r.label in allowed):
            line += " (allowed — --allow-unverified)"
        lines.append(line)
    return lines


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


def check_packet_creds(comp_dir, profile):
    """--packet gate: credentials.txt must carry the packet's published default
    credentials verbatim. In a packet-driven competition the packet IS the credential
    distribution — teams rotate at minute zero — so the guard inverts: instead of
    rejecting known-default literals, every published pair must match exactly."""
    creds = profile.get("credentials") or {}
    credlists = creds.get("credlists") or {}
    expected_pw = creds.get("box_password")
    path = comp_dir / "credentials.txt"
    if not path.exists():
        print("  FAIL  — no credentials.txt to check against the packet")
        return False
    lines = path.read_text().splitlines()
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
        return _pass("packet_accounts", "packet declares no out-of-scope accounts")
    linux_boxes = [b for b in boxes if "win" not in str(b.get("os", "")).lower()]
    if not linux_boxes:
        print("  SKIP  — no Linux box to probe for out-of-scope accounts")
        return _skip("packet_accounts", "no Linux box to probe")
    target = linux_boxes[0]
    probe = "; ".join(
        f"id -u {u} >/dev/null 2>&1 && echo {u}=1 || echo {u}=0" for u in users)
    try:
        proc = ssh_via_gateway(ctx, target["ip"], probe)
    except (CheckError, subprocess.TimeoutExpired) as e:
        print(f"  SKIP  — couldn't probe {target.get('name', target['ip'])} ({e})")
        return _skip("packet_accounts", f"couldn't probe {target.get('name', target['ip'])}")
    if proc.returncode != 0:
        print(f"  SKIP  — probe failed rc={proc.returncode}: "
              f"{(proc.stderr or '').strip()[:120]}")
        return _skip("packet_accounts", f"probe failed rc={proc.returncode}")
    kv = dict(l.split("=", 1) for l in proc.stdout.split() if "=" in l)
    missing = [u for u in users if kv.get(u) != "1"]
    if missing:
        print(f"  FAIL  out-of-scope account(s) missing on "
              f"{target.get('name', target['ip'])}: {', '.join(missing)}")
        return _fail("packet_accounts",
                     f"missing on {target.get('name', target['ip'])}: {', '.join(missing)}")
    print(f"  PASS  {len(users)} out-of-scope account(s) present on "
          f"{target.get('name', target['ip'])}")
    return _pass("packet_accounts", f"{len(users)} account(s) present")


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

    Returns a list of GateResult: the service gate plus (when box_services.json pins
    exist) a separate `pins_registered` result. Every path returns the same shape —
    the old code returned a 2-tuple on the /api/teams failure path while main
    unpacked 3, so a half-deployed or restarting engine (exactly the state this gate
    exists for) crashed verify with ValueError: not enough values to unpack, dumping
    a traceback and suppressing the SUMMARY and every other gate result.

    pins_registered gates the scoreboard's actual ServiceName set against the pins'
    expected set — a pin that never registered (the regression-4x1 same-TYPE collapse:
    12 pins, 11 checks) scores nothing and silent-tallies as UP-absent, so it fails
    the exit code regardless of --strict.

    Under --strict-services nothing-scored is a FAIL, not a vacuous pass (live-found
    2026-10-02: a range where the engine had never scored a round still passed strict
    mode because every service was skipped as "not yet scored"), and the newest scored
    round must be within _FRESHNESS_ROUNDS × Delay — otherwise the scoreboard is frozen
    and an old UP is not a live UP."""
    print("\n[2/5] SERVICES")
    name = "services(strict)" if strict else "services"

    def _pins_status(ok, detail):
        return _bool_gate("pins_registered", ok, detail) if expected_names else None

    if admin_session is None:
        print("  SKIP  — no admin session (login failed).")
        out = [_skip(name, "no admin session (login failed)", gating=strict, label="services")]
        pins = _pins_status(False, "no admin session")
        if pins:
            # Could not be evaluated, not a proven regression — but pins are always
            # gating, so the operator must see SKIP, never a silent pass.
            pins.status = Status.SKIP_UNAVAILABLE
            out.append(pins)
        return out
    try:
        r = admin_session.get(f"{base_url}/api/teams", timeout=10)
        r.raise_for_status()
        api_teams = r.json()
    except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
        print(f"  FAIL  — could not fetch /api/teams: {e}")
        out = [_fail(name, f"could not fetch /api/teams ({str(e)[:60]})",
                     gating=strict, label="services")]
        pins = _pins_status(False, "could not fetch /api/teams")
        if pins:
            out.append(pins)
        return out

    query_ok = True
    all_up = True
    actual_names = set()
    any_service = False
    any_scored = False
    newest_round = None
    for t in api_teams:
        tid, tname = t.get("ID"), t.get("Name", t.get("Identifier"))
        try:
            r = admin_session.get(f"{base_url}/api/services/{tid}", timeout=10)
            r.raise_for_status()
            services = r.json()
        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
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
            name_ = svc.get("ServiceName", "?")
            rounds = svc.get("Last10Rounds") or []
            for rnd in rounds:
                when = _round_start(rnd)
                if when is not None and (newest_round is None or when > newest_round):
                    newest_round = when
            first_round = rounds[0] if rounds else None
            checks = (first_round.get("Checks") if isinstance(first_round, dict) else None) or []
            if not checks:
                unscored += 1
                continue
            any_scored = True
            if all(_check_passed(c) for c in checks):
                up += 1
            else:
                down_names.append(name_)
        total = len(services or []) - unscored
        note = f" ({unscored} not yet scored)" if unscored else ""
        print(f"  {tname}: {up}/{total} services UP{note}")
        for down in down_names:
            print(f"      DOWN: {down}")
        if down_names:
            all_up = False
    if not any_service:
        print("  (no services reported yet — engine may not have scored a round)")
    if strict and not any_service:
        print("  FAIL  --strict-services: the engine reports no services for any team.")
        all_up = False
    elif strict and not any_scored:
        print("  FAIL  --strict-services: no service has been scored yet (the engine may "
              "never have run a round) — an unscored scoreboard is not a passing one.")
        all_up = False
    elif strict and newest_round is not None:
        age_s = (datetime.now(timezone.utc) - newest_round).total_seconds()
        if age_s > _FRESHNESS_ROUNDS * _ROUND_DELAY_SECONDS:
            print(f"  FAIL  --strict-services: newest scored round started "
                  f"{age_s / 60:.0f} min ago (> {_FRESHNESS_ROUNDS}×"
                  f"{_ROUND_DELAY_SECONDS}s) — the scoreboard is stale/frozen, so this "
                  "UP is not a live UP (engine rebooted? the loop does not self-resume).")
            all_up = False
    elif strict and newest_round is None:
        print("  FAIL  --strict-services: no parseable round StartTime in Last10Rounds — "
              "cannot establish the scoreboard is fresh.")
        all_up = False
    if strict and not all_up:
        print("  --strict-services: some services DOWN/unscored/stale -> counts against "
              "exit code")
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
    detail = f"{'UP' if all_up else 'some DOWN/unscored/stale'} " \
             f"({'query ok' if query_ok else 'query failed'})"
    out = [_bool_gate(name, all_up, detail, gating=strict, label="services")]
    pins = _pins_status(pins_ok, f"{len(expected_names)} pinned checks")
    if pins:
        out.append(pins)
    return out


def _team_identifiers(teams):
    """Sorted string identifiers for every team (the third IP octet).

    Tolerates an entry without one: the old `t["identifier"]` raised an uncaught
    KeyError here, killing the whole verifier over a hand-edited teams.json — an
    uncaught crash is the worst failure mode a gate can have."""
    out = set()
    for t in (teams or {}).values():
        ident = t.get("identifier") if isinstance(t, dict) else None
        if ident is not None:
            out.add(str(ident))
    return sorted(out)


def _cidr_covers(cidr, ip):
    """IPv4 CIDR containment — enough for the isolation-rule match."""
    try:
        net_s, bits_s = cidr.split("/")
        bits = int(bits_s)
        net = int(ipaddress.IPv4Address(net_s))
        addr = int(ipaddress.IPv4Address(ip))
    except ValueError:
        return False
    if not 0 <= bits <= 32:
        return False
    mask = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF
    return (net & mask) == (addr & mask)


_ISOLATION_RULE_RE = re.compile(
    r"(?:^|\s)-([sd])\s+(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})(?=\s|$)")


def _has_isolation_rule(stdout, subnet_ips):
    """A FORWARD DROP whose -s AND -d each cover every team subnet.

    The subnets are DERIVED from the team identifiers the rest of the file uses,
    not the hardcoded 192.168.0.0/16 the old match required: a range renumbered
    off that supernet must not read PASS from a match on the old literal (the
    engine's aggregate 192.168.0.0/16 rule covers every derived subnet, so this
    stays a superset check rather than a per-subnet one)."""
    for line in stdout.splitlines():
        if "-j DROP" not in line:
            continue
        sides = {}
        for m in _ISOLATION_RULE_RE.finditer(line):
            sides[m.group(1)] = m.group(2)
        if "s" not in sides or "d" not in sides:
            continue
        if not subnet_ips:
            return True
        if all(_cidr_covers(sides["s"], ip) and _cidr_covers(sides["d"], ip)
               for ip in subnet_ips):
            return True
    return False


def _target_live_from_engine(ctx, to_ip):
    """True/False/None: can the engine itself open to_ip:22?

    The engine's traffic to a directly-attached team subnet is OUTPUT, not FORWARD,
    so the isolation DROP rule cannot apply to it — engine reachability is a
    liveness signal independent of the rule under test."""
    try:
        proc = ssh_to_engine(
            ctx, f"timeout 3 bash -c 'echo > /dev/tcp/{to_ip}/22' 2>/dev/null; echo RC=$?")
    except (CheckError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return "RC=0" in proc.stdout


def check_isolation(ctx, teams, boxes):
    """Confirm isolation DROP rule present and actually blocks cross-team traffic.

    Returns a GateResult. A rule that exists but couldn't be exercised is SKIP,
    never a pass.

    D5 (live-found 2026-10-02): a target box whose sshd is down, whose VM is
    stopped, or that is otherwise unreachable also yields RC!=0 from the
    cross-team /dev/tcp probe, and the old `blocked = "RC=0" not in stdout` read
    that as "blocked as expected" PASS. Establishing "blocked" now additionally
    requires the engine to reach the target (target is alive, so the failure is
    the rule) and the internet control to pass (the documented "blocked while
    that same box can still reach the internet" condition) — otherwise SKIP."""
    print("\n[3/5] ISOLATION")
    identifiers = _team_identifiers(teams)
    subnets = [f"192.168.{i}.1" for i in identifiers]
    try:
        proc = ssh_to_engine(ctx, "sudo iptables -S FORWARD")
    except CheckError as e:
        print(f"  FAIL  — {e}")
        return _fail("isolation", f"could not read FORWARD chain ({str(e)[:60]})")
    if proc.returncode != 0:
        print(f"  FAIL  — could not read FORWARD chain (rc={proc.returncode}): "
              f"{(proc.stderr or '').strip()[:150]}")
        return _fail("isolation", f"could not read FORWARD chain (rc={proc.returncode})")
    if not _has_isolation_rule(proc.stdout, subnets):
        where = ", ".join(subnets) if subnets else "the team subnets"
        print(f"  FAIL  — no DROP rule covering {where} -> {where} in the FORWARD chain; "
              "teams can currently route to each other through the engine.")
        return _fail("isolation", "no team-to-team DROP rule in FORWARD")
    print("  PASS  isolation DROP rule present in FORWARD chain")

    if len(teams) < 2:
        print("  (only 1 team — skipping the cross-team connection test)")
        return _pass("isolation", "rule present (single team — probe not applicable)")

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
        return _pass("isolation", "rule present (couldn't identify two teams' boxes)")
    from_ip, to_ip = (team_ips[i] for i in sorted(team_ips)[:2])

    try:
        proc = ssh_via_gateway(
            ctx, from_ip,
            f"timeout 3 bash -c 'echo > /dev/tcp/{to_ip}/22' 2>/dev/null; echo RC=$?"
        )
    except (CheckError, subprocess.TimeoutExpired) as e:
        print(f"  SKIP  — cross-team connection test couldn't run ({e}); rule-presence "
              "check above passed, but an untested rule is NOT a verified pass.")
        return _skip("isolation", "cross-team probe couldn't run")
    if proc.returncode != 0:
        print(f"  SKIP  — couldn't SSH to {from_ip} to run the test "
              f"(rc={proc.returncode}): {(proc.stderr or '').strip()[:150]}; rule-presence "
              "check above passed, but an untested rule is NOT a verified pass.")
        return _skip("isolation", f"couldn't SSH to {from_ip} to run the probe")

    blocked = "RC=0" not in proc.stdout

    try:
        proc2 = ssh_via_gateway(
            ctx, from_ip,
            "timeout 3 bash -c 'echo > /dev/tcp/1.1.1.1/443' 2>/dev/null; echo RC=$?"
        )
        internet_ok = "RC=0" in proc2.stdout and proc2.returncode == 0
    except (CheckError, subprocess.TimeoutExpired):
        internet_ok = None
    if internet_ok is False:
        print(f"  WARN  {from_ip} can't reach the internet either — the rule (or NAT) may be "
              "over-blocking, not just isolating teams")
    elif internet_ok is None:
        print("  WARN  couldn't run the internet-reachability control check")
    else:
        print(f"  ....  control check ok: {from_ip} can still reach the internet")

    if not blocked:
        print(f"  FAIL  {from_ip} CAN reach {to_ip}:22 — the isolation rule isn't actually "
              "blocking traffic (shadowed or misordered in FORWARD?)")
        return _fail("isolation", f"{from_ip} CAN reach {to_ip}:22")

    target_live = _target_live_from_engine(ctx, to_ip)
    if target_live is not True:
        why = ("the engine can't reach it either, so it is dead/stopped"
               if target_live is False else "target liveness couldn't be established")
        print(f"  SKIP  — {from_ip} can't reach {to_ip}:22, but {why} — a dead box and a "
              "blocked one look identical from here, so this is NOT a verified isolation pass.")
        return _skip("isolation", f"{from_ip}->{to_ip}:22 failed but target liveness unproven")
    if internet_ok is not True:
        print(f"  SKIP  — {from_ip} can't reach {to_ip}:22 and the internet control "
              f"{'failed' if internet_ok is False else 'could not run'} — the rule may be "
              "over-blocking (or the from-box path is broken), not isolating.")
        return _skip("isolation", "target reachable, but the internet control failed")
    print(f"  PASS  {from_ip} cannot reach {to_ip}:22 (blocked as expected; target is live "
          f"and {from_ip} still reaches the internet)")
    return _pass("isolation", f"{from_ip}->{to_ip}:22 blocked, target live, control ok")


def _default_red_seg_ip():
    """--red-seg-ip fallback: bad-auto's config.yaml, else the routed default."""
    cfg_path = REPO_ROOT.parent / "bad-auto" / "config.yaml"
    try:
        deploy = json.loads(cfg_path.read_text()).get("deploy") or {}
        return deploy.get("red_seg_ip") or "10.200.0.10"
    except (OSError, ValueError):
        return "10.200.0.10"


def check_red_identity(ctx, boxes, red_ip, seg_ip, red_user="sysadmin"):
    """--red-identity: prove red's attack traffic arrives at boxes carrying
    its red-segment source address (routed mode), not the team gateway the
    scoring checks source from. Holds a TCP connection open from red01 to a
    Linux box's :22 and reads the box's connection table (ss) while it's up.

    Returns a GateResult; unprovable paths are SKIP, never a pass. Gateway-peer
    lines in the ss output are the verify jump itself (ProxyCommand enters
    through the engine) — expected, not a red sighting."""
    print(f"\n[+RED] RED IDENTITY (routed-mode source address; red01 {red_ip}, "
          f"expecting source {seg_ip})")
    linux_ips = [b["ip"] for b in boxes
                 if b.get("ip") and "win" not in str(b.get("os", "")).lower()]
    if not linux_ips:
        print("  SKIP  — no Linux box to observe the connection from (ss)")
        return _skip("red_identity", "no Linux box to observe from")
    box_ip = linux_ips[0]

    try:
        hold = subprocess.Popen(
            ["ssh", "-i", ctx["ssh_key_path"],
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-o", "ConnectTimeout=10",
             f"{red_user}@{red_ip}",
             f"timeout 25 bash -c 'exec 3<>/dev/tcp/{box_ip}/22; sleep 22'"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        print(f"  SKIP  — couldn't spawn the red01 probe ssh: {e}")
        return _skip("red_identity", "couldn't spawn the red01 probe ssh")

    try:
        observed = ""
        for _ in range(5):
            if hold.poll() is not None:
                break  # probe died early — red01 unreachable or box refused
            time.sleep(2)
            try:
                proc = ssh_via_gateway(
                    ctx, box_ip, "ss -tn state established '( sport = :22 )'",
                    timeout=20)
            except (CheckError, subprocess.TimeoutExpired) as e:
                print(f"  SKIP  — couldn't read {box_ip}'s connection table: {e}")
                return _skip("red_identity", f"couldn't read {box_ip}'s connection table")
            if proc.returncode != 0:
                continue
            observed = proc.stdout
            if seg_ip in observed:
                break
        if hold.poll() is None:
            hold.kill()
    finally:
        hold.wait()

    # rc 0/-9/-15/-None: connected (or we killed the holder mid-sleep — fine).
    # A positive rc means the remote bash died, i.e. the /dev/tcp connect to
    # the box failed — red cannot reach the team subnet at all.
    if hold.returncode is not None and hold.returncode > 0:
        print(f"  FAIL  — red01 could not open the probe connection to "
              f"{box_ip}:22 at all (probe rc={hold.returncode}): red cannot "
              "reach the team subnet, routed firewall rules missing/wrong?")
        return _fail("red_identity", "red01 could not reach the team subnet")
    if seg_ip in observed:
        print(f"  PASS  {seg_ip} visible on {box_ip} as an established :22 peer — "
              "red's source address survives end-to-end")
        return _pass("red_identity", f"{seg_ip} visible end-to-end")
    print(f"  FAIL  — {seg_ip} never appeared among {box_ip}'s established :22 "
          f"peers while red01 held a connection open. Observed peers:\n"
          f"{observed.strip() or '    (none)'}\n"
          "        If the only peers are the team gateway (192.168.<tid>.1), red "
          "is still masqueraded — bad-auto deployed in masq mode or the routed "
          "FORWARD rules are shadowed.")
    return _fail("red_identity", f"{seg_ip} never visible on {box_ip}")


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
    """Report planted raw-socket beacons (informational, not a gate).

    One SSH per Linux box, 30s timeout each; a 4-team x 5-box range is ~20 boxes, i.e.
    up to 10 minutes of sequential waiting for an informational line. The boxes are
    independent and the hop is the engine's ControlMaster-multiplexed channel
    (MaxSessions raised to 64 — utils.run_concurrent's docstring), so it runs on the
    full MAX_CONCURRENCY pool: pure SSH, no Proxmox task, no datastore write. Prints
    stay in the workers under PRINT_LOCK (house pattern: domain_ops/hardening_ops);
    only the informational line order can vary, never a verdict.
    """
    print("\n  (team beacons — informational)")
    linux = [m for m in machines or [] if "win" not in (m.get("os") or "").lower()]
    expected = len(linux)

    def _probe(m):
        ip = m.get("ip")
        name = m.get("name", ip)
        try:
            proc = ssh_via_gateway(
                ctx, ip, "systemctl is-active wda-digest.service 2>/dev/null || true", timeout=30)
        except (subprocess.TimeoutExpired, CheckError) as e:
            with PRINT_LOCK:
                print(f"  WARN  {name} ({ip}): unreachable ({e})")
            return False
        state = (proc.stdout or "").strip()
        if state == "active":
            with PRINT_LOCK:
                print(f"  LIVE  {name} ({ip}) — wda-digest.service active")
            return True
        with PRINT_LOCK:
            print(f"  ....  {name} ({ip}) — no beacon unit running ({state or 'none'})")
        return False

    results = run_concurrent(linux, _probe, max_workers=MAX_CONCURRENCY)
    for r in results:
        # The serial loop only caught TimeoutExpired/CheckError; anything else escaped,
        # and run_concurrent's slot now holds it, so it must escape here too.
        if isinstance(r, Exception):
            raise r
    live = sum(1 for r in results if r is True)
    print(f"  beacons live: {live}/{expected} linux boxes")


def check_misconfig_survival(ctx, boxes):
    """Confirm every team's copy of each box carries same verifiable misconfigs (clone race guard).

    Returns a GateResult: present-on-all PASSes, present-on-some FAILs, and — the
    branch the old code was missing — absent-on-every-team FAILs too (the case
    matched no branch at all, so it printed nothing and left all_ok True). All
    probes unprovable (SSH dead) is SKIP, never a pass."""
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
        return _skip("misconfig_survival",
                     "fewer than 2 teams or nothing shared+verifiable", gating=False)

    all_ok = True
    any_unverified = False
    # Work units in the serial order: groups -> configs -> machines. Each unit is one
    # independent 60s SSH probe, so a groups x configs x machines loop is minutes of
    # sequential waiting. The probes run on the full MAX_CONCURRENCY pool (pure SSH over
    # the ControlMaster channel, no Proxmox task), and the present/absent/unknown
    # classification is aggregated BACK IN SERIAL ORDER below, so every verdict AND
    # every printed line (present on {present} but MISSING on {absent}) is unchanged.

    def _probe(unit):
        config, m = unit
        cmd, predicate = MISCONFIG_CHECKS[config]
        try:
            proc = ssh_via_gateway(ctx, m["ip"], cmd)
        except (subprocess.TimeoutExpired, CheckError):
            return "unknown"
        if proc.returncode != 0:
            return "unknown"
        return "present" if predicate(proc.stdout) else "absent"

    # The serial loop ran (config, machine) pairs in this exact order; the pool keeps
    # that order in its result list, so walk the same nesting and consume in step.
    ordered = [(config, m) for configs, machines in multi_team_groups
               for config in [c for c in configs if c in MISCONFIG_CHECKS]
               for m in machines]
    probe_results = iter(run_concurrent(ordered, _probe, max_workers=MAX_CONCURRENCY))

    def _outcome():
        r = next(probe_results)
        # The serial loop only caught TimeoutExpired/CheckError; any other exception
        # escaped check_misconfig_survival, so a non-caught slot must raise here.
        if isinstance(r, Exception):
            raise r
        return r

    for configs, machines in multi_team_groups:
        verifiable = [c for c in configs if c in MISCONFIG_CHECKS]
        for config in verifiable:
            present, absent, unknown = [], [], []
            for m in machines:
                name = m.get("name", m["ip"])
                outcome = _outcome()
                if outcome == "present":
                    present.append(name)
                elif outcome == "absent":
                    absent.append(name)
                else:
                    unknown.append(name)
            if present and absent:
                all_ok = False
                print(f"  FAIL  '{config}' present on {present} but MISSING on {absent} — "
                      f"didn't survive cloning")
            elif absent and not present:
                # D4: previously this matched no branch — no output, all_ok stayed True.
                # Every team's copy reports the config as absent, so the plant never
                # landed anywhere (or no longer matches); that is not a pass.
                all_ok = False
                note = f" (unverified: {unknown})" if unknown else ""
                print(f"  FAIL  '{config}' absent on every team ({absent}){note} — the "
                      f"config didn't plant on any clone")
            elif present and not absent:
                note = f" (unverified: {unknown})" if unknown else ""
                print(f"  PASS  '{config}' present on all {len(present)} team(s): {present}{note}")
            elif unknown and not present and not absent:
                any_unverified = True
                print(f"  SKIP  '{config}' — could not verify on any team ({unknown})")
    if not all_ok:
        return _fail("misconfig_survival", "misconfig did not survive cloning everywhere")
    if any_unverified:
        return _skip("misconfig_survival", "some configs unprovable (SSH dead)")
    return _pass("misconfig_survival", "verifiable configs present on every team")


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
    and verify reads the LAST SCORED round, reporting stale UP/DOWN as if live.
    /api/engine is snake_case (live-confirmed 2026-09-29): `running` (False =
    paused), `competition_started`, `current_round_time` (RFC3339; the Go zero
    time "0001-01-01T00:00:00Z" means the loop is NOT cycling), and
    `last_round.StartTime`.

    Returns a GateResult and main CONSUMES it: a stopped loop used to WARN and
    return True while main discarded the return value, so a frozen scoreboard
    never affected the exit code. --fix-round-loop runs the two POSTs, but a
    stopped loop is still FAIL for this run (re-run to confirm a fresh round)."""
    print("\n  (scoring round loop — stops silently after an engine reboot)")
    if admin_session is None:
        print("  SKIP  — no admin session.")
        return _skip("round_loop", "no admin session")
    try:
        r = admin_session.get(f"{base_url}/api/engine", timeout=10)
        r.raise_for_status()
        eng = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  WARN  — could not read /api/engine: {e}")
        return _skip("round_loop", "could not read /api/engine")
    if not isinstance(eng, dict):
        return _skip("round_loop", "unexpected /api/engine payload")
    if eng.get("running") is False:
        print("  PASS  — engine paused (round loop not expected to advance)")
        return _pass("round_loop", "engine paused")

    # The judgement itself lives in round_loop.py so the engine-side watchdog
    # (tools/round_loop_guard.py) cannot drift from this gate — two definitions of "the
    # loop is stopped" is precisely the failure mode this repo keeps paying for.
    verdict = round_loop.round_loop_state(eng)
    if verdict["state"] == round_loop.UNKNOWN:
        print("  SKIP  — /api/engine did not answer a usable document")
        return _skip("round_loop", "unexpected /api/engine payload")
    if verdict["state"] == round_loop.ADVANCING:
        print("  PASS  — round loop advancing")
        return _pass("round_loop", "loop advancing")
    if verdict["state"] == round_loop.PENDING:
        print("  PASS  — round loop starting (first round pending within Delay)")
        return _pass("round_loop", "first round pending")
    age_min = (verdict["age_seconds"] or 0) / 60
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
    print("  FAIL  — a frozen scoreboard makes every service verdict above stale; main "
          "now consumes this result, so this run does not pass. Re-run verify once a "
          "fresh round lands.")
    return _fail("round_loop", f"loop stopped; last round {age_min:.0f} min ago")


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


def check_degradations(comp_dir):
    """Surface the prerequisites that failed while the deploy continued anyway.

    The deploy tolerates some failures on purpose (each site has its own reason), but
    it records every one of them in `.deploy_state.json["degradations"]` — apt prep that
    installed nothing, an auth ladder that failed on both transports, a box that never
    settled, a node scan that never happened. Without this line those exist only as
    WARNING text in a scrollback nobody reads, which is how a range can come up "green"
    with every service down. Warning-level, not gating: the deploy did survive them, and
    the plant-coverage/service gates are what decide whether the range actually works.
    """
    state_path = comp_dir / ".deploy_state.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return _skip("degradations", "no readable .deploy_state.json", gating=False)
    entries = state.get("degradations")
    if entries is None:
        print("  SKIP  — this deploy recorded no degradation ledger "
              "(pre-dates the ledger, or state was rewritten).")
        return _skip("degradations", "not recorded by this deploy", gating=False)
    if not entries:
        print("  PASS  — no tolerated failures recorded.")
        return _pass("degradations", "none recorded")
    seen = {}
    for entry in entries:
        seen[entry.get("what", "?")] = seen.get(entry.get("what", "?"), 0) + 1
    detail = ", ".join(f"{what} x{count}" if count > 1 else what
                       for what, count in sorted(seen.items()))
    for entry in entries:
        print(f"  WARN  — {entry.get('what')}: {str(entry.get('detail', ''))[:110]}")
    print(f"  WARN  — {len(entries)} tolerated failure(s) recorded; see "
          f".deploy_state.json['degradations']")
    return _pass("degradations", f"{len(entries)} tolerated: {detail[:150]}")


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
    Returns a SKIP GateResult (gating=False) when the lineup has no domain_roles.json."""
    roles_path = comp_dir / "domain_roles.json"
    if not roles_path.exists():
        print("  SKIP  — no domain_roles.json")
        return _skip("domains", "no domain_roles.json", gating=False)
    try:
        roles = json.loads(roles_path.read_text())
    except (OSError, ValueError) as e:
        print(f"  FAIL  domain_roles.json is unreadable/malformed ({str(e)[:80]})")
        return _fail("domains", "domain_roles.json unreadable/malformed")
    if not isinstance(roles, dict) or not all(
            isinstance(name, str) and isinstance(role, str)
            for name, role in roles.items()):
        print("  FAIL  domain_roles.json must map box names to 'dc' or 'member' strings")
        return _fail("domains", "domain_roles.json schema invalid")
    bad = {name: role for name, role in roles.items() if role not in ("dc", "member")}
    if bad:
        print("  FAIL  domain_roles.json has invalid role value(s): "
              + ", ".join(f"{name}={role!r}" for name, role in sorted(bad.items()))
              + " (expected 'dc' or 'member')")
        return _fail("domains", "domain_roles.json has invalid role values")
    node = os.environ.get("TF_VAR_proxmox_node")
    # Box TYPES (boxes.json order = vmid order), not verify's per-team nakon machines.
    try:
        boxes = json.loads((comp_dir / "boxes.json").read_text())
    except (OSError, ValueError) as e:
        print(f"  FAIL  boxes.json is unreadable/malformed ({str(e)[:80]})")
        return _fail("domains", "boxes.json unreadable/malformed")
    idx = {b["name"]: i for i, b in enumerate(boxes)}
    unknown = [name for name in roles if name not in idx]
    if unknown:
        print("  FAIL  domain_roles.json names box(es) absent from boxes.json: "
              + ", ".join(sorted(unknown)))
        return _fail("domains", "domain_roles.json names unknown box(es)")
    if not teams:
        print("  FAIL  no teams loaded — nothing to check domain roles against")
        return _fail("domains", "no teams loaded")
    dc_name = next((n for n, r in roles.items() if r == "dc"), None)
    ok = True
    domain_sids, machine_sids = {}, {}

    # Work units in the serial loop's exact order (sorted teams, then `roles` order).
    # Each unit is one independent per-VM probe: a Windows guest-agent call with a 120s
    # timeout, or a Linux guest-agent call with a 60s timeout plus a 60s SSH fallback.
    # For 4 teams x 5 boxes that was ~20 SEQUENTIAL probes — 3-10 minutes realistically
    # and up to ~40 minutes in the all-timeout case, which is precisely the
    # half-deployed range this gate exists to catch. Bound MAX_CONCURRENCY (8), not the
    # per-box VM-work bound of 4: these are not Proxmox tasks (each VM has its own
    # virtio-serial agent channel), and utils.run_concurrent's docstring pins 8 as the
    # load-bounded cap that stays far under sshd's raised MaxSessions (64).
    jobs = []
    for team_key, team in sorted(teams.items()):
        ident = team["identifier"]
        domain = team_domain(comp_dir, ident)
        for name, role in roles.items():
            jobs.append({
                "team_key": team_key, "ident": ident, "domain": domain,
                "name": name, "role": role,
                "vmid": vm_id_for(ident, idx[name]),
                "windows": "win" in (boxes[idx[name]].get("template") or "").lower(),
            })

    def _probe(job):
        """One box's probe, returning an outcome instead of printing it.

        The main thread then walks `jobs` in the serial order and aggregates, so every
        verdict, every printed line, and (critically) the team order inside the
        duplicate-DomainSID message stay exactly what the serial loop produced — the
        2026-10-02 tri-state gate contract: same status, same message, same exit code.
        """
        team_key, name = job["team_key"], job["name"]
        ident, domain = job["ident"], job["domain"]
        if job["windows"]:
            try:
                _rc, out, err = guest_agent_exec_windows(node, job["vmid"], _WIN_DOMAIN_PS,
                                                         timeout=120)
                return {"kv": _kv(out), "err": err or "", "info": [], "fail": ""}
            except Exception as e:
                return {"kv": None, "err": "", "info": [],
                        "fail": f"  FAIL  {team_key}/{name}: guest-agent probe failed "
                                f"({str(e)[:80]})"}
        realm_cmd = (f"realm list 2>/dev/null | grep -qi 'domain-name: *{domain}' "
                     f"&& echo JOINED=1 || echo JOINED=0")
        try:
            _rc, out, err = guest_agent_exec_root(node, job["vmid"], realm_cmd, timeout=60)
            return {"kv": _kv(out), "err": err or "", "info": [], "fail": ""}
        except Exception as agent_err:
            # The PVE agent channel has a per-instance exec breaker that can stay
            # tripped (amongus-cde 2026-09-30: airship's failed join probes tripped it
            # permanently). Linux boxes are still reachable over gateway SSH — fall
            # back to it before failing the gate.
            try:
                box_ip = f"192.168.{ident}.{boxes[idx[name]]['last_octet']}"
                proc = ssh_via_gateway(ctx or {"ssh_key_path": str(resolve_ssh_key())},
                                       box_ip, realm_cmd, timeout=60)
                kv = _kv(proc.stdout)
                if kv.get("JOINED") is None:
                    raise CheckError(f"unparseable realm probe: {proc.stdout[:80]}")
                return {"kv": kv, "err": "",
                        "info": [f"  INFO  {team_key}/{name}: agent channel unavailable, "
                                 f"probed over gateway SSH"],
                        "fail": ""}
            except Exception as ssh_err:
                return {"kv": None, "err": "", "info": [],
                        "fail": f"  FAIL  {team_key}/{name}: guest-agent probe failed "
                                f"({str(agent_err)[:80]}) and gateway-SSH fallback failed "
                                f"({str(ssh_err)[:60]})"}

    probe_results = run_concurrent(jobs, _probe, max_workers=MAX_CONCURRENCY)

    for job, outcome in zip(jobs, probe_results):
        team_key, name, role = job["team_key"], job["name"], job["role"]
        domain, windows = job["domain"], job["windows"]
        if isinstance(outcome, Exception):
            # Every probe-body exception was already caught below into a FAIL line;
            # only something outside those handlers could escape, and it still does.
            raise outcome
        for line in outcome["info"]:
            print(line)
        if outcome["fail"]:
            print(outcome["fail"])
            ok = False
            continue
        # `err` is this box's own stderr. The serial loop could leak the PREVIOUS
        # iteration's `err` into the DC detail line when the guest-agent call raised and
        # the SSH fallback then succeeded (the tuple assignment never happened); that
        # leak is not reproduced — it printed another box's stderr, and the verdict in
        # that path is FAIL either way.
        kv, err = outcome["kv"], outcome["err"]
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
            print("  PASS  1 team domain, DomainSID well-formed "
                  "(uniqueness needs a second team to exercise)")
    shared = {sid: v for sid, v in machine_sids.items() if len(v) > 1}
    for sid, v in shared.items():
        print(f"  INFO  member machine SID {sid} shared by {', '.join(v)} "
              f"(linked clones of one golden — harmless for isolated forests)")
    if not ok:
        return _fail("domains", "domain validation failures above")
    return _pass("domains", f"{len(domain_sids)} team domain(s) validated")


def check_plant_coverage(comp_dir):
    """M4 plant-coverage gate: every machine's FULL expected configuration list
    (nakon-config.json) must have actually planted.

    Deploy records failures per machine in .deploy_state.json["plant_coverage_failed"]
    (machine -> [config names whose nakon step reported rc != 0], or every config when
    a machine died before reporting any step). Golden-stage entries map onto every team
    copy of that box (a golden failure means the clones inherited the gap): the golden
    plant records under '{box}-golden' (phase 4, via build_golden_set's coverage
    callback — including alpine_services-tolerated failures), and satellite slots
    record '{box}-golden-slot{N}' since every slot's stage config names its machine
    identically; any slot's failure flags every team copy. This is the backstop that
    catches a broken/undeclared-var config the moment it fails to plant, instead of a
    mid-competition discovery.

    D2 (live-found 2026-10-02): this gate used to read ONLY plant_coverage_failed and
    fail OPEN when it was absent — missing/unparseable state, an older state, or a
    nakon that produced no --json outcome left `failed = {}`, so it printed
    "PASS all N machine(s) report full config coverage" and exited 0 while its own
    SUMMARY said "plant integrity: WARNING — last nakon plant recorded N FAILED
    step(s)". The whole premise of verify (docs/known-issues.md: nakon failures are
    silent by design) was vacuous in exactly that case. It now fails CLOSED: the
    nakon tally deploy.py:105 promises as the fallback is consulted, a non-empty tally
    can never PASS, and when neither source exists the gate FAILs with "coverage was
    never recorded".

    Returns a GateResult (never a bare tuple — the old (checked, ok) arity was part of
    the D1 unpacking crash family)."""
    state_path = comp_dir / ".deploy_state.json"
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {}
    if not isinstance(state, dict):
        state = {}
    # `None` distinguishes "never recorded" from "recorded and clean ({})" — the two
    # must not be conflated, which is what made the old gate fail open.
    failed = state.get("plant_coverage_failed")
    tally = state.get("nakon_failed_steps")
    config_path = comp_dir / "nakon-config.json"
    if not config_path.exists():
        print("  SKIP  — no nakon-config.json (nothing expected).")
        return _skip("plant_coverage", "no nakon-config.json", gating=False)
    try:
        machines = json.loads(config_path.read_text())["machines"]
    except (OSError, ValueError, KeyError) as e:
        print(f"  FAIL  — cannot read nakon-config.json: {e}")
        return _fail("plant_coverage", f"cannot read nakon-config.json ({str(e)[:60]})")

    def cfg_name(c):
        return c if isinstance(c, str) else c["name"]

    recorded = failed if isinstance(failed, dict) else None
    unplanted = {}
    for m in machines:
        name = m.get("name", "?")
        expected = {cfg_name(c) for c in m.get("configurations", [])}
        machine_bad = [c for c in (recorded or {}).get(name) or []]
        # Golden-stage keys: slot 0 records '{box}-golden', satellite slot N records
        # '{box}-golden-slot{N}' (every slot's stage config names its golden machine
        # identically, so the keys must not collide across slots). A failure on ANY
        # slot's golden flags every team copy of the box — each clone inherits its
        # own slot's disk, and a gap on any of them is a range-wide problem.
        base = name.rsplit("-team", 1)[0]
        golden_bad = [c for k, v in (recorded or {}).items()
                      if k == f"{base}-golden" or k.startswith(f"{base}-golden-slot")
                      for c in (v or [])]
        # Intersect each recorded failure with what this machine STILL expects: a
        # failure for a config no longer in its `configurations` is stale and must not
        # fail the gate (amongus-cde-2026 2026-09-30: a recovered SMB v1 entry stayed
        # 'failed' across three green replants). The `<machine ...>` sentinel is kept
        # because it means "died before reporting any step" and is never a config name.
        bad = {c for c in machine_bad if c in expected or c.startswith("<machine")}
        # A golden-stage failure means every team copy inherited the gap.
        bad |= {f"{c} (golden-stage)" for c in golden_bad if c in expected}
        if bad:
            unplanted[name] = sorted(bad)

    problems = []
    if unplanted:
        for name, cfgs in sorted(unplanted.items()):
            print(f"  FAIL  {name}: not planted: {', '.join(cfgs)}")
        problems.append(f"{sum(len(c) for c in unplanted.values())} unplanted config(s)")
    if tally:
        print(f"  FAIL  nakon recorded {len(tally)} FAILED plant step(s): "
              f"{', '.join(str(s)[:80] for s in tally[:3])}"
              f"{' …' if len(tally) > 3 else ''}")
        problems.append(f"{len(tally)} failed nakon step(s)")
    if problems:
        return _fail("plant_coverage", "; ".join(problems))

    if recorded is None:
        if tally is None:
            print("  FAIL  — plant coverage was never recorded: no "
                  "'plant_coverage_failed' in .deploy_state.json and no nakon FAILED "
                  "tally to fall back on (pre-tally deploy?). An unrecorded coverage "
                  "gate is not a passing one.")
            return _fail("plant_coverage", "coverage was never recorded")
        # deploy.py's documented fallback: "no --json outcome (older nakon) —
        # coverage falls back to the tally", and the tally here is present and clean.
        print(f"  PASS  no coverage record (older nakon --json), so coverage falls back "
              f"to the tally: 0 FAILED steps for {len(machines)} machine(s)")
        return _pass("plant_coverage", f"tally clean (no coverage record; {len(machines)} machines)")
    print(f"  PASS  all {len(machines)} machine(s) report full config coverage")
    return _pass("plant_coverage", f"all {len(machines)} machine(s)")


def git_dirty_lines(repo_root=None):
    """Uncommitted paths in this checkout (`git status --porcelain` at the repo root).

    Returns a list of porcelain lines, or None when git cannot answer (not a repo, git
    missing, non-zero rc) — an unverifiable tree must never read as clean. Scoped to
    REPO_ROOT rather than the cwd so the same answer holds wherever verify was invoked."""
    try:
        out = subprocess.run(["git", "status", "--porcelain"],
                             cwd=str(repo_root or REPO_ROOT),
                             capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return [line for line in out.stdout.splitlines() if line.strip()]


def freeze_hashes(comp_dir):
    """The template hashes a freeze would record (None when nothing is recorded yet)."""
    path = comp_dir / ".template-hashes.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def do_freeze(comp_dir, args, gate, coverage_passed):
    """M4 freeze: record the template hashes this verify just passed against, plus the
    code commit, timestamp, and gate results. Preconditions: every gate PASS including
    plant-coverage and services; Windows/domain lineups additionally require the
    operator's --windows-domain-validated attestation (that the run exercised them)."""
    from template_ops import code_path_dirty, git_commit_info
    import time as _time

    hashes = freeze_hashes(comp_dir)
    if not hashes or not (hashes.get("engine") or {}).get("hash") or not hashes.get("golden"):
        print("  FREEZE refused — no template hash record (.template-hashes.json); "
              "deploy once on the M4 pipeline first.")
        return False
    if not coverage_passed:
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
    # A freeze taken while deploy-path CODE is uncommitted pins a commit that does not
    # contain the verified code (the deploy-time check is a warning, not a gate trip — see
    # template_ops.frozen_code_drift). Generated/run state (comp JSON, placement.json,
    # nodes.json, terraform state, .env backups) is expected to be dirty after a run and
    # must not block; it is noted, not refused.
    dirty = git_dirty_lines()
    if dirty is None:
        print("  FREEZE warning — could not read the git worktree state (git unavailable "
              "or not a checkout); the frozen record may not match the code tree.")
    elif dirty:
        code_dirty = [d for d in dirty if code_path_dirty([d])]
        if code_dirty:
            shown = ", ".join(d[:70] for d in code_dirty[:3]) + (" …" if len(code_dirty) > 3 else "")
            print(f"  FREEZE refused — {len(code_dirty)} uncommitted code path(s) in the "
                  f"worktree ({shown}). Freeze LAST, after the final commit: the frozen record "
                  f"pins the commit the run was verified on. To back out before the competition "
                  f"starts: --unfreeze --confirm-unfreeze, commit, then --freeze again.")
            return False
        shown = ", ".join(d[:60] for d in dirty[:3]) + (" …" if len(dirty) > 3 else "")
        print(f"  FREEZE note — {len(dirty)} uncommitted non-code path(s) ignored for the "
              f"freeze ({shown}); only deploy-path code (.py/.tf/.sh/.j2/.ps1) blocks it.")
    record = {
        "frozen_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "code": git_commit_info(),
        "hashes": {"engine": hashes["engine"], "golden": hashes["golden"]},
        "verify_report": {"gates": gate, "plant_coverage": coverage_passed},
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
    parser.add_argument("--allow-unverified", action="append", default=[],
                        dest="allow_unverified", metavar="GATE",
                        help="waive ONE gate's SKIP (could-not-evaluate) verdict so it "
                             "does not fail the exit code; repeatable, e.g. "
                             "--allow-unverified isolation. Without it a gate that "
                             "couldn't run is NOT a pass — a dead SSH must not exit 0.")
    parser.add_argument("--fix-round-loop", action="store_true", dest="fix_round_loop",
                        help="when the scoring round loop looks stopped after an engine "
                             "reboot, run the start/unpause POSTs instead of only warning")
    parser.add_argument("--expect-no-vulns", action="store_true", dest="expect_no_vulns",
                        help="validation comps that deliberately plant zero misconfigurations "
                             "(box_vulns.json all-empty): skip the misconfig gates instead of "
                             "failing on them")
    parser.add_argument("--packet", dest="packet_profile", default=None,
                        help="packet profile (packets/<event>/packet.yaml): adds packet-"
                             "fidelity gates — credentials.txt must match the packet's "
                             "published default credentials, and the packet's out-of-scope "
                             "accounts must exist on the boxes")
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
    parser.add_argument("--red-identity", action="store_true", dest="red_identity",
                        help="also verify red's routed identity: red01 must reach a box with "
                             "its red-segment source IP (not the team gateway). Needs red01 "
                             "deployed; seg IP falls back to ../bad-auto/config.yaml")
    parser.add_argument("--red-ip", default="10.0.0.198", help="red01 mgmt IP for --red-identity")
    parser.add_argument("--red-user", default="sysadmin", help="red01 SSH user for --red-identity")
    parser.add_argument("--red-seg-ip", default=None,
                        help="red01's red-segment address for --red-identity "
                             "(default: ../bad-auto/config.yaml, else 10.200.0.10)")
    parser.add_argument("--timeout", type=int, default=0, metavar="SECONDS",
                        help="overall wall-clock budget for the gate run (default 0 = no "
                             "budget). Checked BETWEEN gates, never mid-flight, so it "
                             "never interrupts a Proxmox task; a gate that never ran is "
                             "SKIP — deliberately non-passing — so a budget can bound the "
                             "run but can never turn an unverified range into a PASS "
                             "(waive with --allow-unverified <gate>).")
    args = parser.parse_args()

    load_dotenv(ENV_PATH)

    # Starts before the terraform/ctx load so the budget covers the whole run; disabled
    # (and therefore inert) at the default --timeout 0.
    budget = RunBudget(args.timeout)

    comp_dir = Path(args.comp_dir).resolve()
    if not comp_dir.is_dir():
        print(f"ERROR: competition directory not found: {comp_dir}", file=sys.stderr)
        return 2

    # Multi-node: activate the recorded placement (env -> engine host, node routes
    # for any node-scoped API call) before anything talks to Proxmox.
    from nodes_ops import activate_placement, read_placement
    placement = read_placement(comp_dir)
    if placement:
        activate_placement(placement)

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

    # Every gate reports a GateResult; the SUMMARY and the exit code are both derived
    # from this list (D6), so a gate's printed word can never disagree with its effect
    # on the verdict.
    results = []
    # Only left None when logins itself was budget-skipped; every consumer below is
    # guarded by the same monotonically expiring budget, so it cannot be reached.
    admin_session = None

    def _spent(name, label=""):
        """--timeout guard, checked BETWEEN gates (never mid-flight): a gate that never
        ran is SKIP_UNAVAILABLE — deliberately non-passing — so a budget can bound the
        run but can never turn an unevaluated range into a PASS. Inert at --timeout 0."""
        if not budget.expired():
            return False
        print(f"  SKIP  — run budget ({budget.label}) exhausted: '{name}' not evaluated")
        results.append(_skip(name, "run budget exhausted before the gate ran", label=label))
        return True

    if not _spent("logins"):
        logins_ok, admin_session = check_logins(base_url, teams, admin_password)
        results.append(_bool_gate("logins", logins_ok))
    print("\n  (default-credential regression guard)")
    if not _spent("no_default_creds"):
        results.append(_bool_gate("no_default_creds", check_no_default_creds(comp_dir)))
    packet_profile = None
    if args.packet_profile:
        try:
            from packet_ops import load_profile
            packet_profile = load_profile(args.packet_profile)
        except SystemExit as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print("\n  (packet fidelity — credentials + out-of-scope accounts)")
        if not _spent("packet_creds"):
            results.append(_bool_gate("packet_creds", check_packet_creds(comp_dir, packet_profile)))
            results.append(check_packet_accounts(ctx, packet_profile, boxes))
    # boxes.json (box TYPES, keyed by name in box_services.json) — nakon-config
    # machines carry team-suffixed names the pin map doesn't use
    try:
        box_list = json.loads((comp_dir / "boxes.json").read_text())
        pinned_services = json.loads((comp_dir / "box_services.json").read_text())
    except (OSError, ValueError):
        box_list, pinned_services = [], {}
    expected_names = expected_service_names(pinned_services, box_list) if pinned_services else set()
    if not _spent("services"):
        results.extend(check_services(
            base_url, admin_session, teams, args.strict_services, expected_names))
    if not _spent("isolation"):
        results.append(check_isolation(ctx, teams, boxes))
    if args.red_identity:
        seg_ip = args.red_seg_ip or _default_red_seg_ip()
        if not _spent("red_identity"):
            try:
                results.append(check_red_identity(ctx, boxes, args.red_ip, seg_ip,
                                                  red_user=args.red_user))
            except CheckError as e:
                print(f"  SKIP  — red identity check couldn't run: {e}")
                results.append(_skip("red_identity", "check couldn't run"))
    print("\n  (live-ops health check status — informational)")
    if not budget.expired():
        report_healthcheck_status(ctx)
    if args.expect_no_vulns:
        print("\n[4/5] MISCONFIG SPOT-CHECK")
        print("  SKIP  — --expect-no-vulns: this comp deliberately plants no misconfigurations")
        note = "--expect-no-vulns: comp plants no misconfigurations"
        results.append(_skip("misconfig", note, gating=False))
        results.append(_skip("misconfig_survival", note, gating=False))
    elif not _spent("misconfig"):
        results.append(_bool_gate("misconfig", check_misconfig(ctx, boxes, comp_dir)))
        results.append(check_misconfig_survival(ctx, boxes))
    if not budget.expired():
        report_beacons(ctx, boxes)
    if not _spent("injects"):
        injects_relevant, injects_ok = check_injects(base_url, admin_session, comp_dir)
        if injects_relevant:
            results.append(_bool_gate("injects", injects_ok))
        else:
            results.append(_skip("injects", "competition ships no injects/ dir", gating=False))
    if not _spent("round_loop"):
        results.append(check_round_loop(base_url, admin_session, fix=args.fix_round_loop))
    print("\n  (M4 plant coverage — expected vs. actually planted, per machine)")
    coverage_result = None
    if not _spent("plant_coverage"):
        coverage_result = check_plant_coverage(comp_dir)
        results.append(coverage_result)
    print("\n  (Tolerated failures — prerequisites that failed while the deploy continued)")
    results.append(check_degradations(comp_dir))
    print("\n  (AD domains — promotion, joins, AD plants, DomainSID uniqueness)")
    if not _spent("domains"):
        results.append(check_domains(comp_dir, teams, boxes, ctx=ctx))

    gate, passed = gate_verdict(results, args.allow_unverified)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for line in summary_lines(results, args.allow_unverified):
        print(line)
    unknown_allowed = sorted(set(args.allow_unverified)
                             - {r.name for r in results} - {r.label for r in results})
    if unknown_allowed:
        print(f"  WARN  --allow-unverified names no gate in this run: "
              f"{', '.join(unknown_allowed)}")
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
              f"FAILED step(s): {', '.join(str(s)[:60] for s in tally[:3])}"
              f"{' …' if len(tally) > 3 else ''}")
    else:
        print("  plant integrity  : last nakon plant recorded 0 FAILED steps")
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

    print("\n" + ("RESULT: PASS — competition looks healthy."
                  if passed else "RESULT: FAIL — see failing checks above."))
    if args.freeze:
        # coverage_result is None only when the budget skipped the gate; a gate that
        # never ran cannot attest coverage, so the freeze is refused (fail-closed).
        ok = do_freeze(comp_dir, args, gate,
                       coverage_result is not None and coverage_result.passed)
        return 0 if (passed and ok) else 1
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
