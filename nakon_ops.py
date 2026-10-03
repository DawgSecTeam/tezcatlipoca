"""Nakon config generation, bundle building, and deployment via scoring engine."""

import fcntl
import contextlib
import json
import math
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from constants import (
    DISRUPTIVE_CONFIGS,
    DOMAIN_INFRA_CONFIGS,
    FINAL_STAGE_CONFIGS,
    GOLDEN_IP_BASE,
    KNOWN_BROKEN_CONFIGS,
    NAKON_DIR,
    PIN_CHECK_OVERRIDES,
    POST_CLONE_CONFIGS,
    REPAIR_STAGE_CONFIGS,
    REQUIRED_VARS,
    SCORING_ENGINE_VMID,
    SLOW_SERVICES,
    WINDOWS_ADMIN_USER,
)
from ssh_ops import _engine_opts
from utils import is_unmanaged
# Single shared definition (windows_ops): nakon's per-machine OS tagging must agree with
# the deploy/golden_ops target split and redeploy's box_platform. This module used to carry
# its own copy plus a private alias, with nothing enforcing that they stayed identical.
from windows_ops import is_windows_template

_ENGINE_LOCKS = {}  # lock path -> open fh (kept referenced so the flock survives)

# M2.4: concurrent domain passes build their single-machine bundles on the operator host.
# The bundle dirs are content-addressed so parallel *different* configs don't collide, but
# the nakon build subprocess itself isn't audited for concurrent temp-state safety — one
# build at a time, deploys still run fully in parallel.
_BUNDLE_BUILD_LOCK = threading.Lock()

# Shell/env names a payload script may reference without any step declaring them.
# The bundle lint (below) fails on any OTHER uppercase reference the step's vars don't
# provide, so a catalog config that starts needing an undeclared var fails at bundle
# build instead of as a mid-plant rc=2 (hosts-redirect-linux / sudoers-rule, 2026-09-24).
_SHELL_VAR_WHITELIST = {
    "PATH", "HOME", "USER", "PWD", "OLDPWD", "SHELL", "TERM", "LANG", "SHLVL",
    "UID", "EUID", "PPID", "IFS", "PS1", "PS4", "HOSTNAME", "RANDOM", "SECONDS",
    "LINENO", "TMPDIR", "MAIL", "OPTARG", "OPTIND", "DEBIAN_FRONTEND",
    "SUDO_USER", "SUDO_UID", "SUDO_GID", "SUDO_COMMAND", "LOGNAME",
}
# Self-defaulting expansions are fine undeclared — strip them before scanning. ALL of
# ${VAR-x} ${VAR:-x} ${VAR=x} ${VAR:=x} ${VAR+x} ${VAR:+x} make the var optional (the
# script supplies/omits a value itself); the colon variants only differ on empty-vs-unset.
# Everything else in braces (${VAR:?required}, ${VAR}, ${VAR%...}, ${VAR#...}) IS a reference:
# live-found 2026-09-25 — sudoers-rule checks "${RULE:?RULE is required}", which the
# brace-closed regex never matched, so the lint silently passed an undeclared var
# and the plant failed rc=2 in 0s. live-found 2026-09-26 — only `:-` was stripped, so the
# common ${VAR-} optional form (local-user GROUPS_ADD, systemd-service PAYLOAD_*/EXTRA_*,
# apache-site EXTRA_DIRECTIVES) false-flagged a valid deploy.
_BASH_DEFAULT_RE = re.compile(r"\$\{[A-Z_][A-Z0-9_]*:?[-+=][^}]*\}")
_BASH_VAR_RE = re.compile(r"\$(?:\{([A-Z_][A-Z0-9_]*)|([A-Z_][A-Z0-9_]*))")


class NakonResult:
    """Structured outcome of one `nakon deploy` (M4 plant-coverage source).

    failed: raw FAILED output lines (the human-facing tally, as before).
    machines: per-machine step results from nakon deploy --json — name, exit_status,
    error, and steps[{name, rc, ...}] — so coverage = expected configs minus rc!=0
    steps, exactly, instead of parsed from interleaved --jobs stdout."""

    def __init__(self, failed, machines):
        self.failed = failed
        self.machines = machines or []

    def __bool__(self):
        return not self.failed

    def failed_configs(self):
        """{machine_name: set(config names whose step rc != 0 or never reported)}."""
        out = {}
        for m in self.machines:
            bad = {s["name"] for s in m.get("steps", []) if s.get("rc") != 0}
            if m.get("error") and not m.get("steps"):
                bad = {"<machine failed before any step>"}
            if bad:
                out[m.get("name") or "?"] = bad
        return out


def _config_name(c):
    return c if isinstance(c, str) else c["name"]


def _pin_key(c):
    """Identity of one pin for dedup: (config name, canonical vars).

    Same-named pins with different vars are DIFFERENT plants — box_baseline.json
    emits 3+ same-named local-user/local-user-win decoy accounts per box, and a cold
    box's otherwise golden-stage configs ride the repair stage into the same combined
    list. Keying on the name alone silently collapsed them (audit-found 2026-10-02:
    .nakon-postclone.json — what redeploy's convergence sweep deploys and verify
    --packet probes — kept 2 of the 4 decoy/plant pins, so a rollback-base sweep
    stopped re-planting packet-promised accounts)."""
    return (_config_name(c),
            json.dumps(c.get("vars") or {}, sort_keys=True) if isinstance(c, dict) else "")


def _cross_box_ip(machines, machine_name, target_box):
    """Same team's copy of target_box's IP, for REQUIRED_VARS "ip:<box>" vars."""
    _, _, identifier = machine_name.rpartition("-team")
    for other in machines:
        box, _, team = other["name"].rpartition("-team")
        if box == target_box and team == identifier:
            return other["ip"]
    raise SystemExit(
        f"  ERROR: cross-box var wants {target_box}'s IP for '{machine_name}' but no "
        f"machine '{target_box}-team{identifier}' exists in nakon-config.json — check "
        f"the box name in the REQUIRED_VARS \"ip:<box>\" kind.")


def _is_identity_kind(kind):
    return str(kind) == "ip" or str(kind).startswith("ip:")


def _identity_banned_configs():
    """Config names whose REQUIRED_VARS carry any identity-dependent kind.

    The single source of truth for BOTH golden paths — slot 0 (generate_stage_configs)
    and every satellite (generate_slot_golden_config). A config whose var is the
    machine's own address ("ip") or a same-team box's address ("ip:<box>") writes that
    address into the disk, so riding a golden bakes the golden's address into every
    linked clone. _golden_stage_machines consults this directly; the two public paths
    can no longer disagree. audit-found 2026-10-02: the satellite path tested
    `"ip" in kinds.values()`, so a cross-box-only ("ip:<box>") config escaped the ban
    while _fill_identity_vars had already baked team1's database IP into the pin —
    every clone on that satellite would have pointed at team1's database. It was
    masked only because every current "ip:<box>" config also happens to be in
    REPAIR_STAGE_CONFIGS."""
    return {name for name, kinds in REQUIRED_VARS.items()
            if any(_is_identity_kind(kind) for kind in kinds.values())}


def _fill_identity_vars(machine, machines):
    """Fill REQUIRED_VARS identity vars in the BASE machine list (generate time).

    The stage-file generation fills per stage too, but the bundle is built from THIS
    list — a config whose script reads an identity var (airship-webapp's $DB_HOST)
    needs it declared here or the bundle lint rejects the whole build."""
    for c in machine["configurations"]:
        if isinstance(c, str):
            continue
        required = REQUIRED_VARS.get(c["name"]) or {}
        if not required:
            continue
        vars_ = c.setdefault("vars", {})
        for var, kind in required.items():
            if not _is_identity_kind(kind):
                continue
            if str(kind) == "ip":
                vars_[var] = machine["ip"]
            else:
                vars_[var] = _cross_box_ip(machines, machine["name"],
                                           str(kind).split(":", 1)[1])


def _validate_pin_vars(configurations, where):
    """Reject bare-name (or var-incomplete) selections of configs that require vars.

    The catalog DB carries no required-vars metadata, so REQUIRED_VARS is the curated
    operator-level source of truth; this turns a mid-plant rc=2 into a generate-time
    error with the exact fix. Identity ("ip") vars are exempt here — they are filled
    per machine at stage-file generation, and their configs are banned from the golden
    stage there."""
    for c in configurations:
        name = _config_name(c)
        required = REQUIRED_VARS.get(name)
        if not required:
            continue
        literals = [v for v, kind in required.items() if kind == "literal"]
        if isinstance(c, str):
            if literals:
                raise SystemExit(
                    f"  ERROR: '{name}' requires var(s) {', '.join(literals)} but is pinned "
                    f"as a bare name in {where}. Pin it as {{\"name\": \"{name}\", \"vars\": {{...}} }} "
                    f"in box_services.json / box_vulns.json — a bare-name plant fails mid-deploy "
                    f"(sudoers-rule rc=2, live-found 2026-09-24).")
        else:
            provided = c.get("vars") or {}
            missing = [v for v in literals if v not in provided]
            if missing:
                raise SystemExit(
                    f"  ERROR: '{name}' is pinned with vars but missing {', '.join(missing)} "
                    f"in {where} — add them to the \"vars\" object.")


def _validate_known_broken_pins(configurations, where, exempt=frozenset()):
    """Reject a *new* pin of a catalog config with a live-confirmed defect.

    constants.KNOWN_BROKEN_CONFIGS is the curated, machine-readable replacement for the
    prose + per-competition-JSON pruning that let a new comp silently re-pin one of these
    (tftpd's Noble dpkg wedge, sshd-force-sftp killing SSH, the Windows user-policy set).
    `exempt` is the escape hatch for a competition that already records the pin — the
    historical comps that predate this gate (cde-2026, pfsense-rvb, scrim-live all pin
    local-user-win) must stay re-deployable; they get a warning instead of an error."""
    for c in configurations:
        name = _config_name(c)
        reason = KNOWN_BROKEN_CONFIGS.get(name)
        if not reason:
            continue
        if name in exempt:
            print(f"  WARNING: '{name}' is a known-broken catalog config, kept because this "
                  f"competition already records it ({where}): {reason}")
            continue
        raise SystemExit(
            f"  ERROR: '{name}' is a known-broken catalog config and cannot be pinned "
            f"({where}): {reason} Unpin it from box_vulns.json/box_services.json "
            f"(the list lives in constants.KNOWN_BROKEN_CONFIGS; it shrinks as upstream "
            f"fixes land — see docs/upstream-defects-handoff.md).")


def _bare_unplantable_reason(name):
    """Why a *freshly randomized* bare-name pin cannot be planted, or None if it can.

    Two classes: a known-broken catalog config (constants.KNOWN_BROKEN_CONFIGS) and a config
    whose REQUIRED_VARS include a literal — randomize returns bare names with no vars, so the
    generate-time var gate below would abort the whole run for a pin nobody chose."""
    if name in KNOWN_BROKEN_CONFIGS:
        return "known-broken"
    required = REQUIRED_VARS.get(name)
    if required and any(kind == "literal" for kind in required.values()):
        return "requires operator vars"
    return None


def _drop_unplantable_bare(names, where):
    """Drop unplantable bare names from a freshly randomized selection, with a notice.

    nakon's randomize (>= d5a443a) already skips configs whose vars it cannot satisfy, but the
    vendored copy may predate that and it has no knowledge of the driver-side broken table, so
    filter here rather than aborting `create-competition` mid-flow. Recorded pins (reuse path)
    are still gated by the two validators below."""
    kept, dropped = [], []
    for c in names:
        name = _config_name(c)
        reason = _bare_unplantable_reason(name)
        if reason:
            dropped.append((name, reason))
        else:
            kept.append(c)
    if dropped:
        detail = "; ".join(f"{n} ({r})" for n, r in dropped)
        print(f"  Skipping {len(dropped)} unplantable bare config(s) in {where}: {detail} — "
              f"pin them explicitly with vars, or see constants.KNOWN_BROKEN_CONFIGS / "
              f"docs/upstream-defects-handoff.md")
    return kept


def build_nakon_bundle(config_path):
    """Build (or reuse) the content-addressed Nakon bundle for this competition."""
    with _BUNDLE_BUILD_LOCK:
        result = subprocess.run(
            [sys.executable, "-m", "nakon", "build",
             "--config", str(Path(config_path).resolve()),
             "--out", "bundles",
             "--json"],
            cwd=str(NAKON_DIR), capture_output=True, text=True, timeout=900,
        )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError(
            "nakon build failed — the vulndb (MySQL + vulndb-ui) has to be reachable from "
            "this machine. Check vendor/nakon/.env."
        )

    info = json.loads(result.stdout.strip().splitlines()[-1])
    state = "cached" if info["cached"] else "fresh"
    print(f"  Nakon bundle {info['bundle_id'][:12]} ({state}, {info['plans']} plan(s), "
          f"{info['machines']} machine(s))")
    bundle_path = NAKON_DIR / info["path"]
    _lint_bundle_vars(bundle_path)
    return bundle_path


def _lint_bundle_vars(bundle_path):
    """Drift guard for the curated REQUIRED_VARS table (M4).

    The catalog DB exposes no required-vars metadata, so the table can silently go
    stale as the catalog grows. The bundle manifest knows exactly which vars each
    step receives and which sha256-addressed script blob it runs — scanning those
    blobs for uppercase `$VAR` references not declared by the step (and not a shell
    builtin) fails the deploy at bundle-build time, before any plant time is spent.
    Remediations: pin the var ({name, vars}), declare it identity-derived in
    REQUIRED_VARS, or (shell builtins only) extend the lint whitelist."""

    def _undeclared(text, provided):
        # Single-quoted spans never expand ('$TTL 604800' is a DNS zone directive, not
        # a var); ${VAR:-default} self-defaults are fine undeclared. The single-quote
        # strip must NOT cross newlines — an apostrophe in a comment (# set the user's
        # wallpaper) otherwise swallows real code below it, including the very VAR=
        # assignment that would exempt a var (live-found 2026-09-26: theme-wallpaper's
        # DEST="$DEST_DIR/..." was eaten, false-flagging DEST).
        # Full-line shell comments never execute — a line whose first non-blank char is
        # `#` is unambiguously a comment (unlike ${VAR#x} or $#, which aren't line-leading).
        # Drop them so prose mentioning a var (theme-motd's `printf '%s\n' "$VAR"` docstring)
        # isn't read as a reference (live-found 2026-09-26).
        text = re.sub(r"(?m)^[ \t]*#.*$", "", text)
        text = re.sub(r"'[^'\n]*'", "''", text)
        # A `[ -z "$VAR" ]` / `[ -n "$VAR" ]` guard (bare or braced, ${VAR-} included) means
        # the script defaults or makes the var optional itself. Scan for it BEFORE stripping
        # self-defaults — otherwise the ${VAR-} inside the guard is removed first and the
        # guard's var is lost (live-found 2026-09-26: GROUPS_ADD/PAYLOAD_PATH/EXTRA_*).
        guarded = {m.group(1) for m in re.finditer(
            r"\[\s+-[zn]\s+\"\$\{?([A-Za-z_][A-Za-z0-9_]*)", text)}
        text = _BASH_DEFAULT_RE.sub("", text)
        # Variables the script assigns itself (IFACE=$(ip route ...)) are internal;
        # leading-underscore names are bash specials or PHP globals ($_GET) in heredocs.
        assigned = {m.group(1) for m in re.finditer(
            r"(?:^|\n)\s*(?:export\s+|readonly\s+|local\s+|declare\s+-?\w*\s+)?"
            r"([A-Za-z_][A-Za-z0-9_]*)=", text)}
        refs = set()
        for m in _BASH_VAR_RE.finditer(text):
            name = m.group(1) or m.group(2)
            if name.startswith("_"):
                continue
            if name in provided or name in _SHELL_VAR_WHITELIST or name in assigned or name in guarded:
                continue
            refs.add(name)
        return refs

    try:
        manifest = json.loads((bundle_path / "manifest.json").read_text())
    except (OSError, ValueError):
        return  # no manifest to lint against (caller fails later on the real run)
    violations = []
    for plan_id, plan in (manifest.get("plans") or {}).items():
        if plan.get("platform") != "linux":
            continue  # windows payloads use different var syntax ($env:, PS casing)
        for step in plan.get("steps", []):
            blob = bundle_path / "blobs" / (step.get("script_sha256") or "")
            try:
                text = blob.read_text(errors="replace")
            except OSError:
                continue
            refs = _undeclared(text, set((step.get("vars") or {}).keys()))
            if refs:
                violations.append((step.get("name") or plan_id[:12], sorted(refs)))
    if violations:
        lines = "\n".join(f"    {name}: {', '.join(vars_)}" for name, vars_ in violations)
        raise SystemExit(
            "  ERROR: catalog payload(s) reference variable(s) their step does not "
            "declare — they would fail mid-plant:\n" + lines +
            "\n  Pin the var(s) in box_services.json/box_vulns.json as "
            '{"name": ..., "vars": {...}}, declare identity-derived ones in '
            "constants.REQUIRED_VARS, or extend the lint whitelist if they are shell "
            "builtins. See docs/known-issues.md 'randomize-to-pin' entry.")


def acquire_engine_lock(engine_vmid=SCORING_ENGINE_VMID):
    """Serialize deploys that share one scoring engine. Since M2.3 the staging slot is
    per-run, but the engine lock still guards the wider shared surface: one engine's
    boxes/engine are being actively reconfigured by whoever holds it.

    Keyed on (proxmox endpoint host, engine vmid) — both known before terraform
    apply — so two different competitions on the same host can't race the same
    engine. Because the key includes the per-competition
    engine vmid, two competitions with DIFFERENT engines (multi-tenant node) take
    distinct locks and run concurrently. Complements _deploy_owner_check (cross-host).
    Idempotent per process; the flock releases when the process exits or crashes."""
    endpoint = os.environ.get("TF_VAR_proxmox_endpoint", "unknown")
    host = re.sub(r"^https?://", "", endpoint).split("/")[0].split(":")[0] or "unknown"
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", f"{host}-{engine_vmid}")
    lock_path = Path.home() / ".tezcatlipoca" / "locks" / f"engine-{slug}.lock"
    if str(lock_path) in _ENGINE_LOCKS:
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            f"  ERROR: another deploy on this host already holds the scoring-engine lock "
            f"({lock_path.name}) — a different competition is targeting the same engine, and "
            "its nakon push would wipe this one's /opt/nakon staging dir. Wait for it (find it "
            "with pgrep -f 'create-competition|redeploy-competition')."
        )
    _ENGINE_LOCKS[str(lock_path)] = fh


def release_engine_lock():
    """Drop every engine flock this process holds. Needed whenever this process is
    about to spawn create-competition as a child (redeploy --reset-event's reseed):
    the child takes the lock itself, so a parent still holding it self-deadlocks
    the reseed (live-found 2026-10-01, cde-2026 reset-event)."""
    for path, fh in list(_ENGINE_LOCKS.items()):
        with contextlib.suppress(OSError):
            fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
        del _ENGINE_LOCKS[path]


def held_lock_paths():
    """The lock files THIS process holds, so a concurrency scan can exclude them."""
    return set(_ENGINE_LOCKS)


def other_deploys_in_flight():
    """Locks held by another LIVE process — i.e. another deploy is running right now.

    Returns [(path, age_seconds), ...] sorted newest first. This is the one concurrency
    signal that cannot lie: a holder is a live process, and the flock is released by the
    kernel when that process dies, so a leftover `.lock` file is never a false positive
    (unlike a timestamp, a VM's existence, or a log's mtime).

    Why it matters (AGENTS.md, docs/environment-facts.md): two sessions driving one
    estate is the documented cause of the 13xx vmid races, the foreign-golden squat, and
    the over-broad sweep that destroyed two other competitions' engines and goldens.
    """
    locks_dir = Path.home() / ".tezcatlipoca" / "locks"
    if not locks_dir.is_dir():
        return []
    ours = held_lock_paths()
    in_flight = []
    for path in sorted(locks_dir.glob("*.lock")):
        if str(path) in ours:
            continue
        try:
            fh = open(path, "a")
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # Someone else is holding it: a deploy is live.
                try:
                    age = max(0.0, time.time() - path.stat().st_mtime)
                except OSError:
                    age = 0.0
                in_flight.append((str(path), age))
            else:
                with contextlib.suppress(OSError):
                    fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()
    in_flight.sort(key=lambda item: item[1])
    return in_flight


def os_to_platform(template):
    """Classify a free-text template name the way nakon does: 'windows' if it has 'win'."""
    return "windows" if "win" in template.lower() else "linux"


def _nakon_randomize(platform, services_budget, vulns_budget):
    """Pick services+vulns via `nakon randomize --json` (cwd=NAKON_DIR for catalog access)."""
    cmd = [
        sys.executable, "-m", "nakon", "randomize",
        "--platform", platform,
        "--services", str(services_budget),
        "--vulns", str(vulns_budget),
        "--exclude", *SLOW_SERVICES,
        "--source", "auto", "--json",
    ]
    result = subprocess.run(cmd, cwd=str(NAKON_DIR), capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError(
            "nakon randomize failed — the vulndb (MySQL + vulndb-ui) has to be reachable from "
            "this machine, or VULNDB_UI_URL set. Check vendor/nakon/.env."
        )
    selection = json.loads(result.stdout.strip().splitlines()[-1])
    return selection["services"], selection["vulns"]


def generate_nakon_config(teams, boxes, difficulty, comp_dir, box_password, box_username="ubuntu"):
    services_path = comp_dir / "box_services.json"
    vulns_path = comp_dir / "box_vulns.json"

    reused = services_path.exists() or vulns_path.exists()
    if reused:
        pinned = json.loads(services_path.read_text()) if services_path.exists() else {}
        pinned_vulns = json.loads(vulns_path.read_text()) if vulns_path.exists() else {}
        box_configs = {
            box["name"]: (pinned.get(box["name"], []), pinned_vulns.get(box["name"], []))
            for box in boxes if not is_unmanaged(box)
        }
        pinned_from = ", ".join(
            p.name for p in (services_path, vulns_path) if p.exists()
        )
        print(f"  Using pinned configurations from {pinned_from}")
        services_path.write_text(
            json.dumps({name: svcs for name, (svcs, _) in box_configs.items()}, indent=2)
        )
        vulns_path.write_text(
            json.dumps({name: vulns for name, (_, vulns) in box_configs.items()}, indent=2)
        )
    else:
        box_configs = {}
        for box in boxes:
            if is_unmanaged(box):
                continue  # firewall/appliance: no scored services, no planted vulns
            platform = os_to_platform(box["template"])
            services, vulns = _nakon_randomize(
                platform, max(math.ceil(difficulty / 3), 1), max(difficulty, 1)
            )
            # randomize cannot know this driver-side broken list, so drop those names here
            # rather than aborting the run in the generate-time gate below.
            where = f"fresh selection for '{box['name']}'"
            services = _drop_unplantable_bare(services, where)
            vulns = _drop_unplantable_bare(vulns, where)
            box_configs[box["name"]] = (services, vulns)

        services_path.write_text(
            json.dumps({name: svcs for name, (svcs, _) in box_configs.items()}, indent=2)
        )
        (comp_dir / "box_vulns.json").write_text(
            json.dumps({name: vulns for name, (_, vulns) in box_configs.items()}, indent=2)
        )


    # Pin gates, at generate time instead of mid-plant:
    #   - var gate (M4): bare-name selections of var-requiring configs (rc=2/rc=127);
    #   - known-broken gate: constants.KNOWN_BROKEN_CONFIGS, the machine-readable
    #     replacement for the per-competition-JSON pruning.
    # Baseline pins (box_baseline.json — packet-promised decoy/local accounts) plant like
    # vulns and get the same gates. On the reuse path every pin is one this competition
    # already records, so the known-broken gate exempts the box (historical comps stay
    # re-deployable); a fresh randomize selection has nothing recorded and is refused.
    baseline_path = comp_dir / "box_baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
    for box_name, (services, vulns) in box_configs.items():
        configs = list(services) + list(vulns) + list(baseline.get(box_name, []))
        where = f"pins for '{box_name}'"
        _validate_pin_vars(configs, where)
        reused_names = {_config_name(c) for c in configs} if reused else frozenset()
        _validate_known_broken_pins(configs, where, exempt=reused_names)

    machines = []
    for i, (team, box) in enumerate(
        ((t, b) for t in teams.values() for b in boxes), start=1
    ):
        if is_unmanaged(box):
            continue  # firewall/appliance: no nakon plant (see utils.is_unmanaged)
        services, vulns = box_configs[box["name"]]
        # Three kinds of pin never ride the machine list:
        #   DOMAIN_INFRA_CONFIGS — domain_ops re-injects them per team with real vars;
        #   score-only pins      — scoring constructs with no catalog config (native
        #                          services like AD's own DNS); quotient/setup builds
        #                          their event.conf check directly;
        #   PIN_CHECK_OVERRIDES  — scoring-only keys on a real plant (strip the keys,
        #                          keep the config). The box_services.json write-back
        #                          above keeps them, so push_event_conf still sees them
        #                          later in the deploy.
        configurations = []
        for c in services + vulns + list(baseline.get(box["name"], [])):
            name = c if isinstance(c, str) else c["name"]
            if name in DOMAIN_INFRA_CONFIGS:
                continue
            if isinstance(c, dict) and c.get("score_only"):
                continue
            configurations.append(
                c if isinstance(c, str)
                else {k: v for k, v in c.items()
                      if k not in PIN_CHECK_OVERRIDES and k != "plant_only"})
        configurations.sort(
            key=lambda c: (c if isinstance(c, str) else c["name"]) in DISRUPTIVE_CONFIGS
        )
        windows = is_windows_template(box["template"])
        machines.append({
            "id": i,
            "name": f"{box['name']}-team{team['identifier']}",
            "ip": f"192.168.{team['identifier']}.{box['last_octet']}",
            "os": box["template"],
            "user": WINDOWS_ADMIN_USER if windows else box_username,
            "password": box_password,
            "configurations": configurations,
        })
    for machine in machines:
        _fill_identity_vars(machine, machines)

    config_path = comp_dir / "nakon-config.json"
    config_path.write_text(json.dumps({"machines": machines}, indent=2))
    os.chmod(config_path, 0o600)
    return config_path


def _golden_stage_machines(full, unbooted, anchor_identifier, box_index_by_name):
    """One machine per box type at the anchor subnet's golden IP. The anchor is
    team1 for slot 0 (historical math) and the satellite's first local team for
    satellite slots — the golden must sit on a bridge that exists on the host
    building it, with that node's jump routing the engine's plant there.

    The identity ban is computed here, not passed in, so slot 0 and every satellite
    share one definition (_identity_banned_configs) and cannot drift apart."""
    identity_banned = _identity_banned_configs()
    seen_types = set()
    golden_machines = []
    for m in full:
        box_name = m["name"].rsplit("-team", 1)[0]
        if (m["ip"].split(".")[2] != str(anchor_identifier) or box_name in seen_types
                or box_name in unbooted):
            continue
        seen_types.add(box_name)
        box_idx = box_index_by_name[box_name]
        golden_kept = [c for c in m["configurations"]
                       if (c if isinstance(c, str) else c["name"]) not in POST_CLONE_CONFIGS]
        banned = sorted({(c if isinstance(c, str) else c["name"]) for c in golden_kept}
                        & identity_banned)
        if banned:
            raise SystemExit(
                f"  ERROR: identity-dependent config(s) {banned} would ride the golden "
                f"disk of '{box_name}' and clone the golden box's address into every "
                f"team. Add them to REPAIR_STAGE_CONFIGS or FINAL_STAGE_CONFIGS "
                f"(constants.py) so they plant per machine post-clone.")
        golden_machines.append({
            **m,
            "id": len(golden_machines) + 1,
            "name": f"{box_name}-golden",
            "ip": f"192.168.{anchor_identifier}.{GOLDEN_IP_BASE + box_idx}",
            "configurations": golden_kept,
        })
    return golden_machines


def generate_slot_golden_config(comp_dir, boxes, unbooted, anchor_identifier, slot):
    """The satellite slot's golden-stage config: identical planted content to slot 0
    (per-box golden hashes stay identical across slots), different transport IPs —
    the anchor team's subnet, reachable from the engine via the jump. Returns the
    path (0600, per-run-secret class)."""
    full = json.loads((comp_dir / "nakon-config.json").read_text())["machines"]
    box_index_by_name = {b["name"]: i for i, b in enumerate(boxes)}
    machines = _golden_stage_machines(full, unbooted, anchor_identifier,
                                      box_index_by_name)
    path = comp_dir / f".nakon-golden-slot{slot}.json"
    path.write_text(json.dumps({"machines": machines}, indent=2))
    os.chmod(path, 0o600)
    return path


def generate_stage_configs(comp_dir, teams, boxes, unbooted=frozenset()):
    """Split the full nakon config into golden / repair / final stage configs (M3.2).

    nakon-config.json (all teams, full configuration lists) stays the source of truth.
    The golden stage takes ONE machine per box type — team1's copy of the full list minus
    the post-clone subsets, placed at the golden IP on team1's subnet — because the golden
    set exists so linked clones inherit the heavy work (package installs, services,
    non-disruptive misconfigs) as bytes on disk. The repair stage (phase 5, before
    domains) and the final stage (phase 6, after domains — see constants for why the
    split matters) carry every team machine with their own subsets; machines with an
    empty subset for a stage are dropped from that stage's file. A combined post-clone
    view (repair ∪ final) is also written for redeploy's convergence sweeps. Returns
    (golden_path, repair_path, final_path, postclone_path); all 0600 and gitignored
    (per-run-secret class — they carry box_password).

    unbooted: box types whose golden stays generalized (domain controllers — see
    golden_ops' identity note). They get no golden machine; everything that would have
    ridden their golden plants per team in the repair stage instead (still pre-domain)."""
    full = json.loads((comp_dir / "nakon-config.json").read_text())["machines"]
    team1_identifier = str(teams["team1"]["identifier"])
    box_index_by_name = {b["name"]: i for i, b in enumerate(boxes)}

    def config_name(c):
        return c if isinstance(c, str) else c["name"]

    # Identity-dependent configs (REQUIRED_VARS "ip"/"ip:<box>" kinds) write an
    # address into the disk. Planted on the golden they would bake the golden's IP
    # into every linked clone, so _golden_stage_machines refuses them there and the
    # repair/final stages fill them per machine instead (see
    # _identity_banned_configs for why this is one shared definition).

    golden_machines = _golden_stage_machines(full, unbooted, team1_identifier,
                                             box_index_by_name)

    def stage_machines(want, include_golden_stage=False):
        out = []
        for m in full:
            cold = m["name"].rsplit("-team", 1)[0] in unbooted
            kept = []
            for c in m["configurations"]:
                name = config_name(c)
                golden_stage = name not in POST_CLONE_CONFIGS
                if name not in want and not (include_golden_stage and cold and golden_stage):
                    continue
                required = REQUIRED_VARS.get(name) or {}
                if required:
                    # Machine identity wins: fill/overwrite "ip"-kind vars with THIS
                    # machine's address, and "ip:<box>"-kind vars with the same team's
                    # copy of that box — whatever the pin said.
                    filled = {v: m["ip"] for v, kind in required.items() if kind == "ip"}
                    for v, kind in required.items():
                        if str(kind).startswith("ip:"):
                            filled[v] = _cross_box_ip(full, m["name"],
                                                      str(kind).split(":", 1)[1])
                    if filled:
                        pinned = (c.get("vars") or {}) if isinstance(c, dict) else {}
                        c = {"name": name, "vars": {**pinned, **filled}}
                kept.append(c)
            if not kept:
                continue
            out.append({**m, "configurations": kept})
        return out

    repair_machines = stage_machines(REPAIR_STAGE_CONFIGS, include_golden_stage=True)
    final_machines = stage_machines(FINAL_STAGE_CONFIGS)
    # Merge repair ∪ final per machine, deduping on the pin's full identity
    # (_pin_key: name + vars) rather than its name alone — a name-only collapse drops
    # packet-promised same-named decoy accounts (audit-found 2026-10-02).
    combined = {}
    for m in repair_machines + final_machines:
        entry = combined.setdefault(m["name"], {**m, "configurations": []})
        seen = {_pin_key(c) for c in entry["configurations"]}
        for c in m["configurations"]:
            key = _pin_key(c)
            if key in seen:
                continue
            seen.add(key)
            entry["configurations"].append(c)
    postclone_machines = sorted(combined.values(), key=lambda m: m["name"])

    written = []
    for suffix, machines in ((".nakon-golden.json", golden_machines),
                             (".nakon-repair.json", repair_machines),
                             (".nakon-final.json", final_machines),
                             (".nakon-postclone.json", postclone_machines)):
        path = comp_dir / suffix
        path.write_text(json.dumps({"machines": machines}, indent=2))
        os.chmod(path, 0o600)
        written.append(path)
    return tuple(written)


def _deploy_owner_check(ssh_base, action="deploy"):
    """Cross-host collision guard. Since M2.3 staging is per-run, same-host concurrent
    runs don't collide — but two operators on two hosts running against the same engine
    still clobber each other's range mid-plant (scrim-extreme-2026-09-20: a second
    operator at 10.0.0.159 ran a full deploy against the same engine). The global
    /opt/nakon/.deploy-owner marker records the last writer; a fresh marker from a
    DIFFERENT hostname means stand down and escalate."""
    import getpass
    me = f"{getpass.getuser()}@{socket.gethostname()}"
    try:
        out = subprocess.run(
            ssh_base + ["cat /opt/nakon/.deploy-owner 2>/dev/null || true"],
            capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        out = ""
    if out:
        try:
            holder, ts = out.split()
            if holder != me and time.time() - float(ts) < 45 * 60:
                raise SystemExit(
                    f"  ERROR: the scoring engine's staging dir is claimed by '{holder}' "
                    f"({int((time.time()-float(ts))/60)} min ago) — refusing to {action} "
                    "concurrently from a different host. Coordinate with that operator "
                    "(ssh engine: sudo cat /opt/nakon/.deploy-owner; sudo rm the marker "
                    "only once they confirm they are done)."
                )
        except ValueError:
            pass
    return me


def _nothing_answered(machines):
    """Why a rc=0 plant applied nothing, or None when the run was real.

    `machines` is nakon deploy --json's per-machine outcome list. A machine that was
    never reached carries no steps at all (`steps: []`) — nakon records the connection
    error in `error` and moves on, so an all-unreachable plant is indistinguishable from
    a clean one by exit code alone.

    Deliberately narrow. It fires only when NO machine produced a single step result:
    a plant where even one host ran one step is a real (if disappointing) outcome and
    must not be escalated, or the floor would abort runs that are merely imperfect —
    exactly the tolerated-failure case the ledger exists for."""
    if not machines:
        return "no per-machine results were reported"
    ran = [m for m in machines if m.get("steps")]
    if ran:
        return None
    names = ", ".join(str(m.get("name") or m.get("ip") or "?") for m in machines[:6])
    return f"all {len(machines)} machine(s) ran zero steps ({names})"


def run_nakon(key, scoring_user, scoring_ip, bundle, config_path, only=None, timeout=2400,
              strict=True, jobs=1, run_tag=None):
    """Push the bundle to the scoring engine and run `nakon deploy` there.

    Returns the FAILED step lines from the deploy output ([] when the plant was
    clean, or when --strict aborted on the first one). jobs > 1 passes nakon's
    --jobs through: per-machine work is atomic in nakon's runner, so machines
    plant in parallel while each machine's steps keep their order.

    Staging is per-run (M2.3): /tmp/nakon-<tag> on the way up, /opt/nakon/<tag> on
    the engine, created and removed only by the run that owns them. Concurrent
    nakon runs (M2.4's parallel domain passes) no longer race on a shared staging
    slot — the global .deploy-owner marker now only arbitrates across HOSTS, since
    same-host runs identify as the same holder."""
    ssh_base = [
        "ssh", "-i", str(key), *_engine_opts(host=scoring_ip),
        f"{scoring_user}@{scoring_ip}",
    ]

    if run_tag is None:
        run_tag = f"{Path(config_path).stem}-{time.strftime('%Y%m%d-%H%M%S')}"
    run_tag = re.sub(r"[^A-Za-z0-9._-]", "_", run_tag)
    tmp_dir = f"/tmp/nakon-{run_tag}"
    opt_dir = f"/opt/nakon/{run_tag}"

    me = _deploy_owner_check(ssh_base, action=f"run Nakon config {Path(config_path).name}")

    subprocess.run(ssh_base + [f"rm -rf {tmp_dir} && mkdir -p {tmp_dir}"],
                   check=True, timeout=60)

    subprocess.run(
        [
            "scp", "-i", str(key), *_engine_opts(host=scoring_ip),
            "-r",
            str(NAKON_DIR / "nakon"),
            str(bundle),
            str(Path(config_path).resolve()),
            f"{scoring_user}@{scoring_ip}:{tmp_dir}/",
        ],
        check=True, timeout=600,
    )

    remote_config = f"{opt_dir}/{Path(config_path).name}"
    only_args = ""
    if only:
        only_args = " --only " + " ".join(shlex.quote(name) for name in only)
    strict_arg = " --strict" if strict else ""
    jobs_arg = f" --jobs {int(jobs)}" if int(jobs) > 1 else ""

    setup_cmd = (
        f"sudo mkdir -p {opt_dir} && "
        f"echo '{me} {int(time.time())}' | sudo tee /opt/nakon/.deploy-owner > /dev/null && "
        f"echo '{me} {int(time.time())}' | sudo tee {opt_dir}/.deploy-owner > /dev/null && "
        f"sudo cp -r {tmp_dir}/. {opt_dir}/ && "
        # paramiko is needed by the remote nakon (root's python3). Check before install:
        # a blind install on every deploy races concurrent runs on the engine's pip.
        "{ sudo python3 -c 'import paramiko' 2>/dev/null || "
        "sudo pip3 install --break-system-packages paramiko; }"
    )
    deploy_cmd = (
        f"cd {opt_dir} && sudo python3 -m nakon deploy "
        f"--bundle {opt_dir}/{bundle.name} --config {remote_config}{only_args}{strict_arg}{jobs_arg}"
        " --json"
    )

    try:
        subprocess.run(ssh_base + [setup_cmd], check=True, timeout=300)
        # A missing archive used to surface as a cryptic 'bundle is missing its plan
        # archive' deep into the plant (scrim-extreme-2026-09-20); prove the staging
        # landed intact before committing hours to it.
        staged = subprocess.run(
            ssh_base + [f"test -s {opt_dir}/{bundle.name} && test -s {remote_config}"],
            capture_output=True, timeout=30)
        if staged.returncode != 0:
            raise RuntimeError(
                f"staged files missing on the engine after the copy "
                f"({opt_dir}/{bundle.name}) — the staging push failed or was wiped; "
                "refusing to start a plant that would die mid-way.")
        failed_steps = []
        machines_json = None
        # Long plants (minutes, over the operator's LAN) sometimes lose the outer
        # ssh session outright — rc=255 with no FAILED step is a transport death,
        # not a plant verdict. Re-run the bundle: steps are idempotent re-plants.
        for attempt in range(1, 4):
            proc = subprocess.Popen(ssh_base + [deploy_cmd], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)

            def _pump():
                nonlocal machines_json
                for line in proc.stdout:
                    print(line, end="")
                    if "FAILED" in line:
                        failed_steps.append(line.strip())
                    # nakon deploy --json prints the structured outcomes as the final
                    # line: one JSON object with a "machines" key. Older nakon versions
                    # (or a failed bootstrap) simply never emit it — coverage then falls
                    # back to the failed-lines tally only.
                    stripped = line.strip()
                    if stripped.startswith("{") and '"machines"' in stripped:
                        try:
                            parsed = json.loads(stripped)
                            if isinstance(parsed.get("machines"), list):
                                machines_json = parsed["machines"]
                        except ValueError:
                            pass

            pump = threading.Thread(target=_pump, daemon=True)
            pump.start()
            try:
                proc.wait(timeout=timeout)
            finally:
                pump.join(timeout=5)
            if proc.returncode == 0:
                # rc=0 is not the same as "the plant did something". A strict=False run
                # whose every machine was unreachable finishes rc=0 with zero FAILED
                # steps, because a host that never answered cannot report a failing
                # step. That is how the scale8 soak's repair sweep "succeeded" against
                # 32 machines that did not exist (2026-10-02) and checkpointed phase 5.
                # 2 flaky steps of 41 is a tolerated failure; 41 of 41 is a broken
                # sweep, and only the latter must stop the run.
                nothing = _nothing_answered(machines_json)
                if nothing:
                    raise RuntimeError(
                        f"nakon deploy reported success but nothing was applied: "
                        f"{nothing}. rc=0 with no per-machine step results means the "
                        f"hosts never answered (or --only selected no machine) — "
                        f"treating that as a completed plant is what let the scale8 "
                        f"soak's repair sweep 'succeed' against machines that did not "
                        f"exist. Refusing to continue; check that the boxes are up and "
                        f"reachable, then re-run."
                    )
                return NakonResult(failed_steps, machines_json)
            if failed_steps or attempt == 3:
                break
            print(f"\n  nakon ssh session died (rc=255, no FAILED steps) — "
                  f"retry {attempt}/3 in 20s...")
            failed_steps = []
            machines_json = None
            time.sleep(20)
        if failed_steps:
            print(f"\n  Nakon plant FAILED steps: {len(failed_steps)}")
            for line in failed_steps[:10]:
                print(f"    {line[:180]}")
        raise RuntimeError(
            f"nakon deploy failed rc={proc.returncode}"
            + (f" ({len(failed_steps)} FAILED steps above)" if failed_steps else ""))
    except subprocess.TimeoutExpired:
        try:
            # Scoped to THIS run's staging path: the remote cmdline carries
            # /opt/nakon/<tag>/, so a concurrent run's deploy (its own tag) survives.
            subprocess.run(ssh_base + [f"sudo pkill -9 -f '{opt_dir}' || true"],
                           timeout=30)
        except Exception:
            pass
        raise
    finally:
        # This run owns its staging dirs; only it removes them.
        try:
            subprocess.run(ssh_base + [f"rm -rf {tmp_dir} && sudo rm -rf {opt_dir}"],
                           check=False, timeout=60)
        except Exception:
            pass


def _run_single_nakon_config(machine, configurations, key, scoring_user, scoring_ip, comp_dir,
                              tag, timeout=1800, strict=True):
    """Deploy one machine with overridden configs in isolation (reboot-safe).

    The config file doubles as a done-marker (deploy_domain_configs skips ADDS
    promotion when .nakon-domain-<team>-adds.json exists), so it is written as
    .pending and renamed into place only after the run succeeds — a failed
    promotion must not read as done on the next resume."""
    tmp_machine = {**machine, "configurations": configurations}
    final_path = comp_dir / f".nakon-domain-{tag}.json"
    pending_path = comp_dir / f".nakon-domain-{tag}.pending.json"
    pending_path.write_text(json.dumps({"machines": [tmp_machine]}, indent=2))
    os.chmod(pending_path, 0o600)
    bundle = build_nakon_bundle(pending_path)
    try:
        run_nakon(key, scoring_user, scoring_ip, bundle, pending_path,
                  only=[machine["name"]], timeout=timeout, strict=strict, run_tag=tag)
    except BaseException:
        pending_path.unlink(missing_ok=True)
        raise
    os.replace(pending_path, final_path)
