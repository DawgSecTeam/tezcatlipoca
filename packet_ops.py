"""Packet profiles: load, validate, compile into competition bundles, render fidelity.

The inverse of generate-packet.py. Competitions publish a packet days ahead carrying
the GENERAL shape — box lineup, scored services/ports, IP scheme, default credentials,
schedule, rules — never the planted misconfigurations. That shape is hand-encoded as
packets/<event>/packet.yaml and compiled here into a fully deployable
competitions/<id>/ bundle (Compfile, boxes.json, users.json, box_services.json,
domain_roles.json, injects/, passwords.json, box_baseline.json,
domain_accounts.json, packet-fidelity.md).

Two boundaries the compiler enforces by omission:
  - box_vulns.json is NEVER emitted — misconfigurations are the black team's secret
    layer, authored after compilation (an all-empty box_vulns.json deploys clean).
  - the fidelity report is the compiler's honesty surface: every packet fact maps to
    exact / substituted / unsupported, so a rehearsal range never silently claims
    fidelity it doesn't have."""

import json
import re
from pathlib import Path

import yaml

from config_ops import random_password
from constants import KNOWN_BROKEN_CONFIGS, MAX_BOXES_PER_TEAM, REQUIRED_VARS
from nakon_ops import os_to_platform
from quotient.setup import _SERVICE_TO_CHECK
from utils import is_legacy_account_name, valid_comp_name, valid_unix_username

REPO_ROOT = Path(__file__).resolve().parent
PACKETS_DIR = REPO_ROOT / "packets"

FIDELITY_LEVELS = ("exact", "substituted", "unsupported")
SCORE_PIN_PREFIX = "score/"
# score/<name> suffix -> Quotient check slice, when the pin doesn't set `check`
_SCORE_CHECK_DEFAULTS = {
    "tcp": "Tcp", "web": "Web", "dns": "Dns", "ssh": "Ssh", "ftp": "Ftp",
    "smtp": "Smtp", "imap": "Imap", "sql": "Sql",
}
_CHECK_SLICES = set(_SCORE_CHECK_DEFAULTS.values())
# Baseline (packet-promised, non-scored) account pins ride box_baseline.json keyed by
# platform; fix_services_on_boxes creates the credlist OS accounts on Linux, so only
# Windows boxes need explicit account pins for credlist users.
_BASELINE_WIN_CONFIG = "local-user-win"
_BASELINE_LINUX_CONFIG = "local-user"

# Decoy baseline passwords come from config_ops.random_password — one canonical
# implementation, not a copy. The 14-char upper+lower+digit+symbol, cmd/PS- and
# URL-safe charset is load-bearing (a digit-free pool got rejected by AD complexity
# policy on every Windows box, scrim-extreme-2026-09-20; URL-specials broke postgres
# DSNs, scrim-extreme-cyberfield-2026-09-22), so two copies could silently drift apart
# on exactly the property that keeps a range deployable. The old local copy claimed to
# spare packet_ops the config_ops -> range_ops -> requests chain, but that isolation was
# already nominal: quotient.setup (imported above) pulls requests regardless.
_gen_password = random_password


def team_domain_parts(name_template):
    """\"mira-{team}.corp.sus\" -> (\"mira-\", \".corp.sus\"); the Compfile
    domain_prefix/domain_suffix knobs domain_ops.team_domain consumes."""
    pieces = str(name_template).split("{team}")
    if len(pieces) != 2:
        raise SystemExit(
            f"  ERROR: domain name_template {name_template!r} must contain exactly one "
            f"'{{team}}' placeholder (e.g. 'mira-{{team}}.corp.sus')")
    return pieces[0], pieces[1]


def _has_control_chars(value):
    """True for newlines / C0 controls / DEL. The Compfile is line-oriented
    (utils.load_compfile splits on newlines into `key value`), so a newline in any
    interpolated field silently truncates the value or injects arbitrary keys —
    a YAML `|` block scalar with a trailing newline is enough (audit-found 2026-10-02:
    event.name/event.scenario/domain.name_template reach compile_profile raw)."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in str(value))


def load_profile(path):
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"  ERROR: packet profile not found: {path}")
    try:
        profile = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise SystemExit(f"  ERROR: {path} is not valid YAML: {e}")
    if not isinstance(profile, dict):
        raise SystemExit(f"  ERROR: {path} must be a YAML mapping")
    profile["_source"] = str(path)
    return profile


def validate_profile(p):
    """Every problem that would die mid-deploy, surfaced at compile time. Returns [str]."""
    errors = []

    def err(msg):
        errors.append(msg)

    event = p.get("event") or {}
    comp_id = str(event.get("comp_id") or "").strip()
    if not comp_id or not valid_comp_name(comp_id):
        err(f"event.comp_id {comp_id!r} must be a valid competition name ([a-z0-9._-])")
    if not (event.get("name") or "").strip():
        err("event.name is required (scoreboard/event title)")
    elif _has_control_chars(event["name"]):
        err(f"event.name {event['name']!r} contains a control character/newline — the "
            "Compfile is line-oriented, so it would inject arbitrary keys or truncate "
            "the title")
    scenario = event.get("scenario")
    if scenario is not None and _has_control_chars(scenario):
        err(f"event.scenario {scenario!r} contains a control character/newline — the "
            "Compfile is line-oriented, so it would inject arbitrary keys or truncate "
            "the scenario")
    difficulty = event.get("difficulty")
    if not isinstance(difficulty, int) or not 1 <= difficulty <= 10:
        err(f"event.difficulty must be an int 1-10 (got {difficulty!r})")

    domain = p.get("domain") or {}
    if domain.get("name_template"):
        if _has_control_chars(domain["name_template"]):
            err(f"domain.name_template {domain['name_template']!r} contains a control "
                "character/newline — it is split into the Compfile domain_prefix/"
                "domain_suffix lines, so it would inject arbitrary keys")
        try:
            team_domain_parts(domain["name_template"])
        except SystemExit as e:
            err(str(e).strip())

    creds = p.get("credentials") or {}
    box_username = creds.get("box_username")
    if not box_username or not valid_unix_username(str(box_username)):
        err(f"credentials.box_username {box_username!r} must match [a-z_][a-z0-9_-]*")
    elif is_legacy_account_name(box_username):
        err(f"credentials.box_username {box_username!r} collides with a legacy distro "
            "system account (cloud-init would adopt it and brick auth)")
    box_password = creds.get("box_password")
    if box_password is not None:
        if not isinstance(box_password, str) or not box_password:
            err(f"credentials.box_password must be a non-empty string "
                f"(got {box_password!r})")
        else:
            # The 8-char floor is a Windows local-account rule (net user / the guest
            # agent push during bootstrap), so it applies to box_password itself, not
            # just the baseline credlist users build_baseline filters (audit-found
            # 2026-10-02). Linux-only lineups have no such floor.
            win_boxes = [b.get("name") for b in (p.get("boxes") or [])
                         if not b.get("unmanaged")
                         and _platform_of(b.get("template")) == "windows"]
            if win_boxes and len(box_password) < 8:
                err(f"credentials.box_password is {len(box_password)} chars but Windows "
                    f"box(es) {win_boxes} need >= 8 — bootstrap's net user/guest-agent "
                    "push dies with InvalidPasswordException (live-found 2026-09-30)")
    out_of_scope = creds.get("out_of_scope")
    if out_of_scope is not None:
        if not isinstance(out_of_scope, list):
            err(f"credentials.out_of_scope must be a list of usernames "
                f"(got {type(out_of_scope).__name__})")
        else:
            # These become local-user/local-user-win USERNAME vars on every managed
            # box (build_baseline), so they get the same name rules as credlist users.
            for user in out_of_scope:
                if not valid_unix_username(str(user)):
                    err(f"credentials.out_of_scope: username {user!r} must match "
                        "[a-z_][a-z0-9_-]*")
                elif is_legacy_account_name(str(user)):
                    err(f"credentials.out_of_scope: {user!r} collides with a legacy "
                        "distro system account")
    accounts = creds.get("domain_accounts")
    if accounts is not None:
        if not isinstance(accounts, list):
            err(f"credentials.domain_accounts must be a list of "
                f"{{username, password, ...}} mappings (got {type(accounts).__name__})")
        else:
            for i, acct in enumerate(accounts):
                where = f"credentials.domain_accounts[{i}]"
                if not isinstance(acct, dict):
                    err(f"{where} must be a mapping with username/password "
                        f"(got {acct!r})")
                    continue
                # domain_ops adds each account at phase 6 (acct["username"] /
                # acct["password"]), after DC promotion — a missing key is a KeyError
                # ~40 minutes into a live deploy. AD names may be mixed-case (CDE's
                # Red/Blue/...), so only presence/type is checked, not the unix regex.
                if not isinstance(acct.get("username"), str) or not acct["username"].strip():
                    err(f"{where}.username is required and must be a non-empty string "
                        f"(got {acct.get('username')!r})")
                if not isinstance(acct.get("password"), str) or not acct["password"]:
                    err(f"{where}.password is required and must be a non-empty string "
                        f"(got {acct.get('password')!r})")
    credlists = creds.get("credlists") or {}
    unknown_lists = set(credlists) - {"linux", "domain"}
    if unknown_lists:
        err(f"credentials.credlists has unknown list(s) {sorted(unknown_lists)} "
            "(supported: linux, domain)")
    for name, pairs in credlists.items():
        if not isinstance(pairs, dict) or not pairs:
            err(f"credentials.credlists.{name} must be a non-empty user->password mapping")
            continue
        for user in pairs:
            if not valid_unix_username(str(user)):
                err(f"credentials.credlists.{name}: username {user!r} must match "
                    "[a-z_][a-z0-9_-]*")
            elif is_legacy_account_name(str(user)):
                err(f"credentials.credlists.{name}: {user!r} collides with a legacy "
                    "distro system account")
    if credlists.get("domain") and not domain.get("name_template"):
        err("credentials.credlists.domain needs domain.name_template (the domain "
            "credlist authenticates against the per-team AD forest)")

    boxes = p.get("boxes") or []
    if not 1 <= len(boxes) <= MAX_BOXES_PER_TEAM:
        err(f"boxes: need 1-{MAX_BOXES_PER_TEAM} box types (got {len(boxes)})")
    names, octets = set(), set()
    dc_boxes = []
    fw_boxes = []
    for b in boxes:
        name = b.get("name")
        if not name or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", str(name)):
            err(f"box name {name!r} must match [a-z0-9][a-z0-9._-]*")
        elif name in names:
            err(f"duplicate box name {name!r}")
        names.add(name)
        lo = b.get("last_octet")
        if not isinstance(lo, int) or not 1 <= lo <= 254:
            err(f"box {name!r}: last_octet must be an int 1-254 (got {lo!r})")
        elif lo in octets:
            err(f"box {name!r}: last_octet {lo} already used by another box")
        octets.add(lo)
        if not (b.get("template") or "").strip():
            err(f"box {name!r}: template is required (must match a tagged Proxmox VM)")
        if b.get("fidelity") not in FIDELITY_LEVELS:
            err(f"box {name!r}: fidelity must be one of {FIDELITY_LEVELS} — say what the "
                "packet promised vs what this template delivers")
        if b.get("in_path") and not b.get("unmanaged"):
            err(f"box {name!r}: in_path firewalls are unmanaged by definition — set "
                "`unmanaged: true` alongside `in_path: true`")
        if b.get("in_path") and lo != 1:
            err(f"box {name!r}: an in-path firewall owns the team gateway address — "
                f"last_octet must be 1 (got {lo})")
        if lo == 1 and not b.get("unmanaged"):
            # An unmanaged fw01 at .1 is valid: cde-2026/maccdc packets carry one
            # as a packet-fidelity stand-in (idle while the engine holds the
            # gateway). Managed hosts must never claim the gateway address, though.
            err(f"box {name!r}: last_octet 1 is the team gateway — only an unmanaged "
                "firewall/appliance (in_path or a packet stand-in) may sit there")
        if b.get("in_path"):
            fw_boxes.append(name)
        role = b.get("domain_role")
        if b.get("unmanaged"):
            if role:
                err(f"box {name!r}: unmanaged boxes cannot take a domain_role")
            if any(s.get("box") == name for s in p.get("services") or []):
                err(f"box {name!r}: unmanaged boxes cannot carry scored services")
        elif role not in (None, "dc", "member"):
            err(f"box {name!r}: domain_role must be 'dc', 'member', or absent")
        if role == "dc":
            dc_boxes.append(name)
        for key in ("cpu", "memory_mb"):
            if key in b and (not isinstance(b[key], int) or b[key] < 1):
                err(f"box {name!r}: {key} must be a positive int")
        if "disk_gb" in b and b["disk_gb"] is not None and \
                (not isinstance(b["disk_gb"], int) or b["disk_gb"] < 1):
            err(f"box {name!r}: disk_gb must be a positive int or null (template's own)")
    if len(dc_boxes) > 1:
        err(f"domain_role 'dc' appears on {len(dc_boxes)} boxes {dc_boxes} — exactly one DC")
    if len(fw_boxes) > 1:
        err(f"in_path firewalls appear on {len(fw_boxes)} boxes {fw_boxes} — one per-team "
            "lineup (the transit design routes every team through a single gateway)")

    box_names = names
    displays_by_box = {}
    for s in p.get("services") or []:
        box = s.get("box")
        if box not in box_names:
            err(f"service {s.get('name')!r}: unknown box {box!r}")
            continue
        box_entry = next((b for b in boxes if b.get("name") == box), {})
        if box_entry.get("unmanaged"):
            err(f"service {s.get('name')!r}: box {box!r} is unmanaged")
        if not (s.get("name") or "").strip():
            err(f"service on {box!r}: name is required")
        port = s.get("port")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            err(f"service {s.get('name')!r}: port must be an int 1-65535 (got {port!r})")
        plant_only = s.get("scored") is False
        pin = s.get("pin")
        if not pin:
            err(f"service {s.get('name')!r}: pin is required (catalog config name, or "
                f"'{SCORE_PIN_PREFIX}<check>' for a native service scored without a plant)")
        elif plant_only:
            pass  # a real plant with no scored check — a companion score-only pin scores it
        elif str(pin).startswith(SCORE_PIN_PREFIX):
            suffix = str(pin)[len(SCORE_PIN_PREFIX):]
            check = s.get("check") or _SCORE_CHECK_DEFAULTS.get(suffix)
            if check not in _CHECK_SLICES:
                err(f"service {s.get('name')!r}: score-only pin {pin!r} has no resolvable "
                    f"check ({check!r}) — set `check:` to one of {sorted(_CHECK_SLICES)}")
        elif str(pin) not in _SERVICE_TO_CHECK:
            err(f"service {s.get('name')!r}: pin {pin!r} has no Quotient check mapping — it "
                f"would plant but score nothing. Fix the name, use a score-only pin "
                f"({SCORE_PIN_PREFIX}<check>), or mark it `scored: false` (plant-only)")
        # Known-broken gate: same list the deploy-time pin gate enforces
        # (nakon_ops._validate_known_broken_pins). A packet that pins one of these
        # would compile into a bundle the deploy then refuses, so fail at compile time.
        broken = KNOWN_BROKEN_CONFIGS.get(str(pin))
        if broken:
            err(f"service {s.get('name')!r}: pin {pin!r} is a known-broken catalog config: "
                f"{broken}")
        # fidelity is required like boxes[].fidelity: the honesty report defaults a
        # missing value to "exact" (render_fidelity), silently claiming parity the
        # packet may not have (audit-found 2026-10-02).
        fidelity = s.get("fidelity")
        if fidelity not in FIDELITY_LEVELS:
            err(f"service {s.get('name')!r}: fidelity must be one of {FIDELITY_LEVELS} "
                f"(got {fidelity!r}) — the honesty report would otherwise claim 'exact'")
        svc_vars = s.get("vars")
        if svc_vars is not None and not isinstance(svc_vars, dict):
            err(f"service {s.get('name')!r}: vars must be a mapping (got {svc_vars!r})")
        # REQUIRED_VARS "literal" vars must be pinned at compile time, not discovered
        # at deploy start (nakon_ops._validate_pin_vars) after the golden is built.
        # Identity kinds are exempt: they are auto-filled per machine.
        required = REQUIRED_VARS.get(str(pin)) or {}
        missing = [v for v, kind in required.items()
                   if kind == "literal" and v not in (svc_vars or {})]
        if missing:
            err(f"service {s.get('name')!r}: pin {pin!r} needs var(s) {missing} — add "
                f"them to this service's `vars:` (a bare pin fails mid-plant rc=2)")
        display = (s.get("display") or "").strip()
        if not display and not plant_only:
            err(f"service {s.get('name')!r}: display is required (scoreboard uniqueness "
                "is <box>-<Display>; the compiler does not guess)")
        dual = s.get("dual_credit")
        if dual:
            if not credlists.get("domain"):
                err(f"service {s.get('name')!r}: dual_credit needs "
                    "credentials.credlists.domain")
            if display.endswith("-domain"):
                err(f"service {s.get('name')!r}: display {display!r} would collide with "
                    "its own domain-credit twin (<display>-domain)")
        displays = displays_by_box.setdefault(box, [])
        if not display:
            continue
        for d in ([display, f"{display}-domain"] if dual else [display]):
            if d in displays:
                err(f"box {box!r}: two services render scoreboard name {box}-{d} — "
                    "Quotient requires unique <box>-<Display> per box")
            displays.append(d)

    injects = p.get("injects") or []
    slugs = set()
    for inj in injects:
        slug = str(inj.get("slug") or "").strip()
        if not slug or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", slug):
            err(f"inject slug {slug!r} must match [a-z0-9][a-z0-9._-]*")
        elif slug in slugs:
            err(f"duplicate inject slug {slug!r}")
        slugs.add(slug)
        if not (inj.get("title") or "").strip():
            err(f"inject {slug!r}: title is required")
        offsets = [inj.get(k, 0) for k in ("open_offset_min", "due_offset_min",
                                           "close_offset_min")]
        if offsets != sorted(offsets):
            err(f"inject {slug!r}: offsets must be open <= due <= close (got {offsets})")

    return errors


def _service_pins(svc):
    """box_services.json pin(s) for one profile service. Score-only services become
    {'name': 'score/<x>', 'score_only': True, ...}; plant-only services ({"scored":
    false}) become {'name': ..., 'plant_only': True} — a real catalog plant with no
    scored check (the service is scored by a companion score-only pin). Normal pins
    carry the display override, and dual_credit adds the domain-credlist twin (2:1
    full/half emulation: both checks score 5, so local-only scores half of both-up)."""
    pin = svc["pin"]
    display = svc.get("display")
    out = []
    if svc.get("scored") is False:
        entry = {"name": pin, "plant_only": True}
        if svc.get("vars"):
            entry["vars"] = dict(svc["vars"])
        out.append(entry)
        return out
    if str(pin).startswith(SCORE_PIN_PREFIX):
        suffix = str(pin)[len(SCORE_PIN_PREFIX):]
        check = svc.get("check") or _SCORE_CHECK_DEFAULTS.get(suffix)
        if check not in _CHECK_SLICES:
            raise SystemExit(
                f"  ERROR: service {svc.get('name')!r}: score-only check {check!r} must be "
                f"one of {sorted(_CHECK_SLICES)} (set `check:` explicitly)")
        entry = {"name": str(pin), "score_only": True, "check": check,
                 "display": display, "port": svc["port"]}
        out.append(entry)
        return out
    entry = {"name": pin}
    if display:
        entry["display"] = display
    if svc.get("vars"):
        entry["vars"] = dict(svc["vars"])
    for key in ("port", "path", "scheme", "status"):
        if svc.get(key) is not None:
            entry[key] = svc[key]
    if not svc.get("dual_credit"):
        out.append(entry)
        return out
    out.append(entry)
    twin = {"name": pin, "credlist": "domain", "display": f"{display}-domain"}
    if entry.get("port") is not None:
        twin["port"] = entry["port"]
    out.append(twin)
    return out


def _cred_carrying_services(p, box_name):
    """True when any service on the box resolves to a check that authenticates with a
    credlist — Windows boxes then need baseline local-user-win pins for those users."""
    from quotient.setup import _SERVICE_TO_CHECK
    for svc in p.get("services") or []:
        if svc.get("box") != box_name:
            continue
        pin = str(svc.get("pin") or "")
        if pin.startswith(SCORE_PIN_PREFIX):
            if (_SCORE_CHECK_DEFAULTS.get(pin[len(SCORE_PIN_PREFIX):])
                    or svc.get("check")) in {"Ssh", "Ftp", "Smtp", "Imap", "Sql"}:
                return True
            continue
        check = _SERVICE_TO_CHECK.get(pin)
        if check and check[0] in {"Ssh", "Ftp", "Smtp", "Imap", "Sql"}:
            return True
    return False


def _platform_of(template):
    """Packet-side wrapper over the one template->platform map (nakon_ops.os_to_platform).

    The shared map calls `.lower()` straight on its argument; packet YAML tolerates a box
    with no `template` key, so coerce first. This wrapper historically read
    `str(template)`, so an absent key classified as "linux" instead of raising — that
    coercion is the only intentional difference and it is preserved here (for every str
    input the two are identical). Nothing else may re-implement the mapping: the packet
    compiler's Windows/Linux split here and nakon's machine tagging have to agree
    (audit 2026-10-02)."""
    return os_to_platform(str(template))


def build_baseline(p):
    """box_baseline.json payload: {box: [pins]}.

    Two kinds of packet-promised, non-scored accounts ride here (planted like vulns
    ride box_vulns.json, but published in the packet, so they are baseline not secret):
      - out-of-scope decoys (scorebot/blackteam/red_scoring) on every managed box;
      - credlist local users on WINDOWS boxes (Linux gets them from
        fix_services_on_boxes; IIS FTP authenticates against local Windows users).
    Decoy passwords are random (the packet never publishes them) and live only in
    this 0600, gitignored file."""
    creds = p.get("credentials") or {}
    out_of_scope = list((creds.get("out_of_scope") or []))
    credlist_users = sorted((creds.get("credlists") or {}).get("linux") or {})
    credlists_cfg = creds.get("credlists") or {}
    baseline = {}
    for b in p.get("boxes") or []:
        if b.get("unmanaged"):
            continue
        pins = []
        platform = _platform_of(b.get("template"))
        win_cfg, linux_cfg = (_BASELINE_WIN_CONFIG, _BASELINE_LINUX_CONFIG)
        for user in out_of_scope:
            if platform == "windows":
                # New-LocalUser caps -Description at 48 chars (Windows API) — live-found
                # 2026-09-30: 54-char descriptions died in ParameterBindingValidation.
                pins.append({"name": win_cfg, "vars": {
                    "USERNAME": user, "PASSWORD": _gen_password(),
                    "FULLNAME": "Scoring infrastructure",
                    "DESCRIPTION": f"{user} - out of scope, do not touch"}})
            else:
                pins.append({"name": linux_cfg, "vars": {
                    "USERNAME": user, "PASSWORD": _gen_password()}})
        if platform == "windows" and credlist_users and _cred_carrying_services(p, b["name"]):
            for user in credlist_users:
                # Windows local-account creation enforces a minimum password length
                # (live-found 2026-09-30: airship/airship died InvalidPasswordException
                # while blueteam/n0t_sus1 passed — 8 chars is the floor). A credlist
                # user whose packet password is too short stays MySQL/OS-only on Linux
                # (fix_services_on_boxes has no such floor); the engine's cred-carrying
                # check iterates the whole credlist, so one valid Windows account is
                # enough for the check to score.
                if len(str(credlists_cfg["linux"][user])) < 8:
                    continue
                pins.append({"name": win_cfg, "vars": {
                    "USERNAME": user, "PASSWORD": credlists_cfg["linux"][user],
                    "FULLNAME": "Service account",
                    "DESCRIPTION": "scoring credlist account (packet defaults)"}})
        if pins:
            baseline[b["name"]] = pins
    return baseline


def compile_profile(profile_path, competitions_dir=None, force=False, dry_run=False):
    """Validate + emit the competition bundle. Returns (comp_dir, fidelity_md, wrote[])."""
    p = load_profile(profile_path)
    errors = validate_profile(p)
    if errors:
        raise SystemExit("  ERROR: packet profile failed validation:\n" +
                         "\n".join(f"    - {e}" for e in errors))

    event = p["event"]
    comp_id = event["comp_id"]
    comps_root = Path(competitions_dir) if competitions_dir else REPO_ROOT / "competitions"
    comp_dir = comps_root / comp_id
    authored = (comp_dir / "Compfile").exists() and (comp_dir / "boxes.json").exists()
    if authored and not force and not dry_run:
        raise SystemExit(
            f"  ERROR: competitions/{comp_id} already holds an authored bundle — "
            f"pass --force to overwrite the authored files (deploy artifacts and "
            f".deploy_state.json are left alone), or pick another event.comp_id.")

    creds = p.get("credentials") or {}
    credlists = creds.get("credlists") or {}
    linux_creds = dict(credlists.get("linux") or {})
    domain_creds = dict(credlists.get("domain") or {})
    if (p.get("domain") or {}).get("name_template"):
        prefix, suffix = team_domain_parts(p["domain"]["name_template"])
    else:
        prefix, suffix = "team", ".local"  # historical default; explicit in the Compfile

    boxes = []
    for b in p["boxes"]:
        entry = {"name": b["name"], "last_octet": b["last_octet"],
                 "cpu": b.get("cpu", 1), "memory_mb": b.get("memory_mb", 2048),
                 "disk_gb": b.get("disk_gb"), "template": b["template"]}
        if b.get("disk_iface"):
            entry["disk_iface"] = b["disk_iface"]
        if b.get("unmanaged"):
            entry["unmanaged"] = True
        boxes.append(entry)

    services_by_box = {b["name"]: [] for b in boxes}
    for s in p.get("services") or []:
        services_by_box[s["box"]].extend(_service_pins(s))

    domain_roles = {b["name"]: b["domain_role"] for b in p["boxes"]
                    if b.get("domain_role")}

    files = {}
    files["Compfile"] = "\n".join([
        f"name {event['name']}",
        f"scenario {event.get('scenario', '').strip() or event['name']}",
        f"difficulty {event['difficulty']}",
        f"domain_prefix {prefix}",
        f"domain_suffix {suffix}",
        f"packet_source {(Path(p['_source']).relative_to(REPO_ROOT) if Path(p['_source']).is_relative_to(REPO_ROOT) else p['_source'])}",
    ]) + "\n"
    files["boxes.json"] = json.dumps(boxes, indent=2)
    files["users.json"] = json.dumps({
        "box_username": creds.get("box_username") or "ubuntu",
        "credlist_usernames": list(linux_creds),
    }, indent=2)
    files["box_services.json"] = json.dumps(
        {name: pins for name, pins in services_by_box.items()}, indent=2)
    if domain_roles:
        files["domain_roles.json"] = json.dumps(domain_roles, indent=2)
    if creds.get("box_password") or linux_creds or domain_creds:
        pw = {"credlists": {}}
        if creds.get("box_password"):
            pw["box_password"] = creds["box_password"]
        if linux_creds:
            pw["credlists"]["linux"] = linux_creds
        if domain_creds:
            pw["credlists"]["domain"] = domain_creds
        files["passwords.json"] = json.dumps(pw, indent=2)
    if creds.get("domain_accounts"):
        files["domain_accounts.json"] = json.dumps(
            {"accounts": creds["domain_accounts"]}, indent=2)
    baseline = build_baseline(p)
    if baseline:
        files["box_baseline.json"] = json.dumps(baseline, indent=2)

    fidelity = render_fidelity(p)

    wrote = []
    if not dry_run:
        comp_dir.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            path = comp_dir / name
            path.write_text(content + ("" if content.endswith("\n") else "\n"))
            wrote.append(path)
        for inj in p.get("injects") or []:
            inj_dir = comp_dir / "injects" / inj["slug"]
            inj_dir.mkdir(parents=True, exist_ok=True)
            meta = {"title": inj["title"],
                    "open_offset_min": inj.get("open_offset_min", 0),
                    "due_offset_min": inj.get("due_offset_min", 60),
                    "close_offset_min": inj.get("close_offset_min", 90)}
            if inj.get("briefing"):
                (inj_dir / "briefing.md").write_text(inj["briefing"])
                meta["description_file"] = "briefing.md"
            else:
                meta["description"] = inj.get("description", "")
            (inj_dir / "inject.json").write_text(json.dumps(meta, indent=2) + "\n")
            wrote.append(inj_dir / "inject.json")
        (comp_dir / "packet-fidelity.md").write_text(fidelity)
        wrote.append(comp_dir / "packet-fidelity.md")
        for secret in ("passwords.json", "domain_accounts.json", "box_baseline.json"):
            if (comp_dir / secret).exists():
                (comp_dir / secret).chmod(0o600)
    return comp_dir, fidelity, wrote


def _md_escape(text):
    return str(text).replace("|", "\\|")


def _display_source(p):
    src = Path(p["_source"])
    rel = str(src.relative_to(REPO_ROOT)) if src.is_relative_to(REPO_ROOT) else str(src)
    return rel


def render_fidelity(p):
    """packet-fidelity.md — the honesty surface. What the packet promised vs what this
    range delivers, per box / service / credential / schedule fact."""
    event = p["event"]
    lines = [
        f"# Packet fidelity — {event['name']}",
        "",
        f"Compiled from `{_display_source(p)}`"
        + (f" (packet: {p['packet']['source']})" if p.get("packet", {}).get("source") else ""),
        "",
        "Statuses: **exact** = delivered as promised · **substituted** = equivalent "
        "capability, different implementation · **unsupported** = promised shape the "
        "pipeline cannot express (documented, not silently dropped).",
        "",
        "## Boxes",
        "",
        "| Box | Packet says | Built as | Status | Note |",
        "|---|---|---|---|---|",
    ]
    for b in p["boxes"]:
        built = f"`{b['template']}`" + (" (unmanaged)" if b.get("unmanaged") else "")
        note = b.get("note", "")
        lines.append(f"| {b['name']} | {_md_escape(b.get('packet_os', '—'))} | {built} "
                     f"| {b['fidelity']} | {_md_escape(note)} |")

    lines += ["", "## Scored services", "",
              "| Box | Packet service | Port | Check | Status | Note |", "|---|---|---|---|---|---|"]
    for s in p.get("services") or []:
        pin = str(s["pin"])
        if pin.startswith(SCORE_PIN_PREFIX):
            check = f"score-only {s.get('check') or pin[len(SCORE_PIN_PREFIX):]}"
        else:
            check = f"`{pin}`"
        if s.get("dual_credit"):
            check += " + domain-credlist twin"
        lines.append(f"| {s['box']} | {_md_escape(s['name'])} | {s['port']} | {check} "
                     f"| {s.get('fidelity', 'exact')} | {_md_escape(s.get('note', ''))} |")

    creds = p.get("credentials") or {}
    credlists = creds.get("credlists") or {}
    lines += ["", "## Credentials", ""]
    if creds.get("note"):
        lines.append(f"- Note: {creds['note']}")
    if creds.get("box_password"):
        lines.append(f"- Box login `{creds.get('box_username')}` uses the packet-published "
                     "password (passwords.json, 0600 + gitignored) — teams change it at "
                     "minute zero, exactly like the real event.")
    for name, pairs in credlists.items():
        lines.append(f"- Scoring credlist `{name}.credlist`: "
                     + ", ".join(f"`{u}`" for u in pairs))
    for acct in creds.get("domain_accounts") or []:
        admin = " (domain admin)" if acct.get("admin") else ""
        lines.append(f"- AD account `{acct['username']}`{admin} planted per team via "
                     "domain_accounts.json")
    for user in creds.get("out_of_scope") or []:
        lines.append(f"- Out-of-scope decoy `{user}` planted on every managed box with a "
                     "random password (packet rule: these accounts exist, are never used "
                     "for harm, and must not be touched)")

    scoring = (p.get("scoring") or {}).get("weights") or {}
    if scoring:
        lines += [
            "",
            "## Scoring model",
            "",
            f"- Packet weights: {scoring}. The engine scores flat 5 pts/check/round — "
            "uptime weight is what the engine measures; injects are graded by white team "
            "over Quotient submissions; the red-team component is scored from bad-auto "
            "evidence, not the scoreboard.",
        ]
        if any(s.get("dual_credit") for s in p.get("services") or []):
            lines.append(
                "- Dual-account credit (domain full / local half) is emulated with paired "
                "checks at 5 pts each: both-up = 10, local-only = 5 — the packet's 2:1 "
                "ratio, at the cost of doubled scoreboard rows on those services.")

    schedule = p.get("schedule") or []
    if schedule:
        lines += ["", "## Schedule (offsets from T0)", "",
                  "| Window | Minute (T0+) | Note |", "|---|---|---|"]
        for w in schedule:
            at = w.get("at_min")
            lines.append(f"| {w.get('label', '—')} | {at if at is not None else '—'} "
                         f"| {_md_escape(w.get('note', ''))} |")
        lines.append("")
        lines.append("`run-schedule.py` executes the freeze/resume windows against the "
                     "live engine; engine pausing is the only schedule primitive.")

    gaps = [b for b in p["boxes"] if b.get("fidelity") == "unsupported"]
    gaps += [s for s in p.get("services") or [] if s.get("fidelity") == "unsupported"]
    lines += ["", "## Gaps", ""]
    if gaps:
        for g in gaps:
            label = g.get("name") or g.get("packet_os")
            lines.append(f"- UNSUPPORTED: {label} — {g.get('note', '')}")
    else:
        lines.append("- none recorded (see substituted entries above for the honest deltas)")
    return "\n".join(lines) + "\n"
