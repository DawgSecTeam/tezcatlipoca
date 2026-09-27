"""Nakon config generation, bundle building, and deployment via scoring engine."""

import fcntl
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
    FINAL_STAGE_CONFIGS,
    GOLDEN_IP_BASE,
    NAKON_DIR,
    POST_CLONE_CONFIGS,
    REPAIR_STAGE_CONFIGS,
    REQUIRED_VARS,
    SCORING_ENGINE_VMID,
    SLOW_SERVICES,
    WINDOWS_ADMIN_USER,
)
from ssh_ops import _engine_opts

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


def _is_windows_template(template_name):
    return "win" in template_name.lower()


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

    if services_path.exists() or vulns_path.exists():
        pinned = json.loads(services_path.read_text()) if services_path.exists() else {}
        pinned_vulns = json.loads(vulns_path.read_text()) if vulns_path.exists() else {}
        box_configs = {
            box["name"]: (pinned.get(box["name"], []), pinned_vulns.get(box["name"], []))
            for box in boxes
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
            platform = os_to_platform(box["template"])
            services, vulns = _nakon_randomize(
                platform, max(math.ceil(difficulty / 3), 1), max(difficulty, 1)
            )
            box_configs[box["name"]] = (services, vulns)

        services_path.write_text(
            json.dumps({name: svcs for name, (svcs, _) in box_configs.items()}, indent=2)
        )
        (comp_dir / "box_vulns.json").write_text(
            json.dumps({name: vulns for name, (_, vulns) in box_configs.items()}, indent=2)
        )


    # Pin-var gate (M4): bare-name selections of var-requiring configs die here, at
    # generate time, instead of as a mid-plant rc=2/rc=127.
    for box_name, (services, vulns) in box_configs.items():
        _validate_pin_vars(list(services) + list(vulns), f"pins for '{box_name}'")

    machines = []
    for i, (team, box) in enumerate(
        ((t, b) for t in teams.values() for b in boxes), start=1
    ):
        services, vulns = box_configs[box["name"]]
        configurations = services + vulns
        configurations.sort(
            key=lambda c: (c if isinstance(c, str) else c["name"]) in DISRUPTIVE_CONFIGS
        )
        windows = _is_windows_template(box["template"])
        machines.append({
            "id": i,
            "name": f"{box['name']}-team{team['identifier']}",
            "ip": f"192.168.{team['identifier']}.{box['last_octet']}",
            "os": box["template"],
            "user": WINDOWS_ADMIN_USER if windows else box_username,
            "password": box_password,
            "configurations": configurations,
        })

    config_path = comp_dir / "nakon-config.json"
    config_path.write_text(json.dumps({"machines": machines}, indent=2))
    os.chmod(config_path, 0o600)
    return config_path


def generate_stage_configs(comp_dir, teams, boxes, box_username="ubuntu", unbooted=frozenset()):
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

    # Identity-dependent configs (REQUIRED_VARS "ip" kind) write the machine's own
    # address into the disk. Planted on the golden they would bake the golden's IP
    # into every linked clone, so generate refuses them there and fills them per
    # machine in the repair/final stages instead.
    identity_banned = {name for name, kinds in REQUIRED_VARS.items() if "ip" in kinds.values()}

    seen_types = set()
    golden_machines = []
    for m in full:
        box_name = m["name"].rsplit("-team", 1)[0]
        if (m["ip"].split(".")[2] != team1_identifier or box_name in seen_types
                or box_name in unbooted):
            continue
        seen_types.add(box_name)
        box_idx = box_index_by_name[box_name]
        golden_kept = [c for c in m["configurations"]
                       if config_name(c) not in POST_CLONE_CONFIGS]
        banned = sorted({config_name(c) for c in golden_kept} & identity_banned)
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
            "ip": f"192.168.{team1_identifier}.{GOLDEN_IP_BASE + box_idx}",
            "configurations": golden_kept,
        })

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
                    # machine's address, whatever the pin said.
                    filled = {v: m["ip"] for v, kind in required.items() if kind == "ip"}
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
    combined = {}
    for m in repair_machines + final_machines:
        entry = combined.setdefault(m["name"], {**m, "configurations": []})
        entry["configurations"].extend(
            c for c in m["configurations"]
            if config_name(c) not in {config_name(x) for x in entry["configurations"]})
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
        if failed_steps:
            print(f"\n  Nakon plant FAILED steps: {len(failed_steps)}")
            for line in failed_steps[:10]:
                print(f"    {line[:180]}")
        if proc.returncode != 0:
            raise RuntimeError(
                f"nakon deploy failed rc={proc.returncode}"
                + (f" ({len(failed_steps)} FAILED steps above)" if failed_steps else ""))
        return NakonResult(failed_steps, machines_json)
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


def is_windows_template(template_name):
    return _is_windows_template(template_name)
