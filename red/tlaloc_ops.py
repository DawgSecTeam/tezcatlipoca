"""tlaloc as the range's red team — deploy, seed, verdict, destroy (phase 7)."""

import io
import json
import os
import secrets
import subprocess
import tarfile
from pathlib import Path

from common.constants import red_bridge_name
from common.utils import competition_name, local_python

REPO = Path(__file__).resolve().parents[1]
TLALOC_REPO = REPO / "tlaloc"

RED_USER = "sysadmin"
INSTALL_DIR = "/opt/tlaloc"
CONFIG_PATH = "/etc/tlaloc/config.yaml"
STATE_DIR = "/var/lib/tlaloc"
INTEL_DIR = "/var/lib/tlaloc/intel"
UNIT = "tlaloc.service"
UNIT_SRC = TLALOC_REPO / "systemd" / "tlaloc.service"
# red01 hosts Mythic; the C2 images overran a beacon-sized disk and Docker never started.
DEFAULT_RED_DISK_GB = 60

CONFIG_KEYS = ("assume_breach", "depth", "duration_min", "ladder", "caps",
               "opening_takedown_min", "windows", "budget")
TAKEDOWN_MODES = ("stop", "stop_mask", "stop_rename", "stop_break_config",
                  "stop_rename_config", "block_port", "recon")


def compfile_lines(comp_dir):
    out = {}
    try:
        text = (Path(comp_dir) / "Compfile").read_text()
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or " " not in line:
            continue
        key, value = line.split(" ", 1)
        out[key] = value.strip()
    return out


def _coerce(text):
    text = text.strip()
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            continue
    return text


def _set_dotted(target, dotted, value):
    node = target
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def red_block(comp_dir):
    block = {}
    lines = compfile_lines(comp_dir)
    if "red_yaml" in lines:
        import yaml
        path = Path(comp_dir) / lines["red_yaml"]
        if path.exists():
            loaded = yaml.safe_load(path.read_text()) or {}
            if isinstance(loaded, dict):
                block.update(loaded)
    for key, value in lines.items():
        if key.startswith("red."):
            _set_dotted(block, key[len("red."):], _coerce(value))
    # A range kept up past red.duration_min goes quiet — the loop exits cleanly — and the
    # Compfile is graded input, so the extension comes from the environment.
    over = os.environ.get("TEZ_TLALOC_DURATION_MIN")
    if over:
        try:
            block["duration_min"] = float(over)
        except ValueError:
            raise RuntimeError(f"TEZ_TLALOC_DURATION_MIN must be minutes, got {over!r}")
    return block


def red_disk_gb(comp_dir, default=None):
    raw = compfile_value(comp_dir, "red.disk_gb")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else int(default or DEFAULT_RED_DISK_GB)


def compfile_value(comp_dir, key):
    return compfile_lines(comp_dir).get(key, "")


def validate_red_block(block):
    problems = []
    mode = block.get("assume_breach", "full")
    if mode not in ("full", "light", "off"):
        problems.append({"field": "red.assume_breach",
                         "problem": f"must be full|light|off, got {mode!r}"})
    depth = block.get("depth", 3)
    if not isinstance(depth, int) or not 0 <= depth <= 3:
        problems.append({"field": "red.depth", "problem": "must be 0..3"})
    duration = block.get("duration_min")
    if duration is not None and (not isinstance(duration, (int, float)) or duration <= 0):
        problems.append({"field": "red.duration_min",
                         "problem": "must be a positive number of minutes"})
    tiers = ((block.get("ladder") or {}).get("tiers")) or []
    last = -1.0
    for tier in tiers:
        name = tier.get("name", "?")
        pct = tier.get("at_pct")
        if pct is None and "at_min" not in tier:
            problems.append({"field": f"red.ladder.tiers[{name}]",
                             "problem": "needs at_pct or at_min"})
            continue
        if pct is not None:
            if not 0.0 <= float(pct) <= 1.0:
                problems.append({"field": f"red.ladder.tiers[{name}]",
                                 "problem": "at_pct must be 0..1"})
            elif float(pct) < last:
                problems.append({"field": f"red.ladder.tiers[{name}]",
                                 "problem": "tiers must be sorted by time"})
            else:
                last = float(pct)
        unknown = [m for m in (tier.get("modes") or []) if m not in TAKEDOWN_MODES]
        if unknown:
            problems.append({"field": f"red.ladder.tiers[{name}]",
                             "problem": f"unknown mechanism(s) {unknown}"})
    caps = block.get("caps") or {}
    if caps and int(caps.get("min_standing", 1)) < 1:
        problems.append({"field": "red.caps.min_standing",
                         "problem": "must be >= 1 (the pin needs one reachable service)"})
    engine = str(block.get("c2_engine") or "sliver").strip().lower()
    if engine not in ("sliver", "mythic"):
        problems.append({"field": "red.c2_engine",
                         "problem": f"must be sliver|mythic, got {engine!r}"})
    spread = str(block.get("c2_implant_spread") or "draw").strip().lower()
    if spread not in ("draw", "full"):
        problems.append({"field": "red.c2_implant_spread",
                         "problem": f"must be draw|full, got {spread!r}"})
    return problems


def tlaloc_config(comp_dir, block=None, *, comp_id=None, engine_ip=None,
                  red_ip=None, quotient_url=None, clef_url=None, c2_url=None,
                  c2_operation=None, c2=None, llm=None):
    """The body of /etc/tlaloc/config.yaml — a diff against tlaloc's DEFAULTS."""
    block = block if block is not None else red_block(comp_dir)
    comp_dir = Path(comp_dir).resolve()
    cfg = {
        "run": {"comp": comp_id or competition_name(comp_dir),
                "comp_dir": INTEL_DIR,
                "state_dir": STATE_DIR,
                "kill_file": f"{STATE_DIR}/KILL",
                "dry_run": False,
                "host_guard": True},
        "red": {k: v for k, v in block.items() if k in CONFIG_KEYS},
    }
    if "assume_breach" not in cfg["red"]:
        cfg["red"]["assume_breach"] = "full"
    if "depth" not in cfg["red"]:
        cfg["red"]["depth"] = 3
    if engine_ip or quotient_url:
        cfg["sensors"] = {"quotient_enabled": True,
                          "quotient_url": quotient_url or f"http://{engine_ip}"}
    if clef_url:
        cfg.setdefault("clef", {}).update({
            "base_url": clef_url,
            "model": str(block.get("clef_model") or "clef-flash")})
    if c2_url:
        cfg.setdefault("c2", {}).update({"enabled": True, "base_url": c2_url,
                                         "operation": c2_operation or cfg["run"]["comp"]})
    if c2:
        cfg.setdefault("c2", {}).update(c2)
    # red.c2_implant_spread full: every implant surface on every box, not the per-plant draw.
    spread = str(block.get("c2_implant_spread") or "").strip()
    if spread:
        cfg.setdefault("c2", {})["implant_spread"] = spread
    if llm:
        cfg["llm"] = dict(llm)
    return cfg


def write_config(comp_dir, path=None, **kwargs):
    import yaml
    target = Path(path) if path else Path(comp_dir) / ".tlaloc-config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(tlaloc_config(comp_dir, **kwargs),
                                     sort_keys=False))
    target.chmod(0o600)
    return target


def contract_fields(runner=None):
    runner = runner or _default_runner
    proc = runner([local_python(), "-m", "tlaloc", "verify-config",
                   "--print-contract"], cwd=str(TLALOC_REPO), capture_output=True,
                  text=True, timeout=60)
    if getattr(proc, "returncode", 1) != 0:
        return []
    try:
        return json.loads(getattr(proc, "stdout", "") or "[]")
    except ValueError:
        return []


def _default_runner(cmd, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


def ssh_argv(ssh_key, red_ip, remote, timeout=30):
    return ["ssh", "-i", str(ssh_key), "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={timeout}", f"{RED_USER}@{red_ip}", remote]


def stage_command(comp_dir):
    comp_dir = Path(comp_dir).resolve()
    return (
        f"sudo -n mkdir -p {INSTALL_DIR} {INTEL_DIR} {STATE_DIR} && "
        f"sudo -n chown -R {RED_USER}: {STATE_DIR} {INTEL_DIR} && "
        f"sudo -n tar -xzf - -C {INSTALL_DIR}")


def seed_command():
    """cd and --comp are load-bearing: the package is staged, and blind intel still exits 0."""
    # Source c2.env when it exists — the Mythic operator password arrives that way;
    # Sliver's operator profile is a file the config names, so no env is needed.
    return (f"cd {INSTALL_DIR} && sudo -n bash -c 'set -a; "
            f"[ -f /etc/tlaloc/c2.env ] && . /etc/tlaloc/c2.env; "
            f"set +a; python3 -m tlaloc --config {CONFIG_PATH} --comp {INTEL_DIR} seed'")


def comp_prep_remote_command(transports="all"):
    """Runs ON red01, where the operator profile and the target intel live."""
    return (f"sudo -n rm -rf /root/.sliver/slivers && cd {INSTALL_DIR} && "
            f"sudo -n python3 -m tlaloc --config {CONFIG_PATH} "
            f"--comp {INTEL_DIR} c2-prep --transports {transports}")


def payloads_ready(ssh_key, red_ip, *, runner=None):
    """A resume/scrim reuses the matrix already built on red01; rebuilding 30 implants is hours."""
    remote = (f"cd {INTEL_DIR}/red/payloads 2>/dev/null && sudo -n python3 -c "
              "\"import json,os; d=json.load(open('manifest.json')); "
              "assert d.get('boxes'); "
              "assert all(os.path.isfile(e['file']) for e in d['boxes'].values())\"")
    proc = _ssh_run(ssh_key, red_ip, remote, runner=runner, timeout=120)
    return getattr(proc, "returncode", 1) == 0


def pull_payload_manifest(ssh_key, red_ip, comp_dir, *, runner=None):
    dest = Path(comp_dir) / "red" / "payloads"
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / "manifest.json"
    proc = _ssh_run(ssh_key, red_ip, f"sudo -n cat {INTEL_DIR}/red/payloads/manifest.json",
                    runner=runner, timeout=120)
    if getattr(proc, "returncode", 1) != 0 or not getattr(proc, "stdout", ""):
        raise RuntimeError(f"c2-prep wrote no payload manifest on red01 "
                           f"({_tail(proc)})")
    target.write_text(proc.stdout)
    try:
        boxes = (json.loads(proc.stdout) or {}).get("boxes") or {}
    except ValueError as exc:
        raise RuntimeError(f"c2-prep's manifest on red01 is not JSON: {exc}") from exc
    if not boxes:
        raise RuntimeError("c2-prep's manifest lists no payloads — the seed would "
                           "plant nothing")
    return {"path": str(target), "payloads": len(boxes)}


def plant_command():
    return (f"cd {INSTALL_DIR} && sudo -n python3 -m tlaloc "
            f"--config {CONFIG_PATH} --comp {INTEL_DIR} run")


def report_command():
    return (f"cd {INSTALL_DIR} && sudo -n python3 -m tlaloc "
            f"--config {CONFIG_PATH} --comp {INTEL_DIR} report")


def unit_install_command(local_unit=None):
    unit = Path(local_unit) if local_unit else UNIT_SRC
    return (f"sudo -n tee /etc/systemd/system/{UNIT} < {INSTALL_DIR}/systemd/"
            f"{unit.name} >/dev/null && sudo -n systemctl daemon-reload && "
            f"sudo -n systemctl enable --now {UNIT}")


def _env_pairs(body):
    pairs = {}
    for line in (body or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pairs[key.strip()] = value
    return pairs


def comp_prep_command(comp_dir, transports=("http", "webhook")):
    comp_dir = Path(comp_dir).resolve()
    # --state-dir: c2-prep runs on the operator, where the config's VM path is unwritable.
    return [local_python(), "-m", "tlaloc", "--comp", str(comp_dir),
            "--config", str(comp_dir / ".tlaloc-config.yaml"),
            "--state-dir", str(comp_dir / ".tlaloc-state"), "c2-prep",
            "--transports", ",".join(transports)]


def unit_stop_command():
    return f"sudo -n systemctl disable --now {UNIT} || true"


def _ssh_run(ssh_key, red_ip, remote, *, runner=None, timeout=900, input=None):
    runner = runner or _default_runner
    kwargs = {"timeout": timeout}
    if input is not None:
        kwargs["input"] = input
    try:
        return runner(ssh_argv(ssh_key, red_ip, remote), **kwargs)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([], 124, "", f"timeout after {timeout}s")


def _tail(proc, n=300):
    return (((getattr(proc, "stderr", "") or "") + (getattr(proc, "stdout", "") or ""))[-n:])


def require_frozen_payloads(comp_dir):
    payloads = Path(comp_dir) / "red" / "payloads"
    manifest_path = payloads / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"C2 is enabled but comp-prep left no readable frozen payload manifest "
            f"at {manifest_path} — run comp-prep (`tlaloc c2-prep`) before the deploy"
        ) from exc
    boxes = (manifest.get("boxes") if isinstance(manifest, dict) else None) or {}
    if not boxes:
        raise RuntimeError(
            f"comp-prep's manifest at {manifest_path} lists no boxes — the seed would "
            f"plant nothing; re-run comp-prep (`tlaloc c2-prep`)")
    missing = sorted(str(entry.get("file")) for entry in boxes.values()
                     if not (payloads / str(entry.get("file") or "")).is_file())
    if missing:
        raise RuntimeError(
            f"comp-prep's manifest references missing payload file(s) "
            f"{', '.join(missing)} under {payloads} — re-run comp-prep "
            f"(`tlaloc c2-prep`)")


def stage_red01(ssh_key, red_ip, comp_dir, *, runner=None, timeout=600, c2=False):
    comp_dir = Path(comp_dir).resolve()
    if c2:
        require_frozen_payloads(comp_dir)
    if not (TLALOC_REPO / "tlaloc" / "__init__.py").exists():
        raise RuntimeError(f"no tlaloc agent at {TLALOC_REPO}")
    if not UNIT_SRC.exists():
        raise RuntimeError(f"no systemd unit at {UNIT_SRC}")
    tar = subprocess.run(["tar", "-czf", "-", "-C", str(TLALOC_REPO),
                          "tlaloc", "systemd", "config.example.yaml"],
                         capture_output=True)
    if tar.returncode != 0:
        raise RuntimeError("tar of the tlaloc folder failed")
    runner = runner or _default_runner
    remote = (f"sudo -n mkdir -p {INSTALL_DIR} {INTEL_DIR} {STATE_DIR} && "
              f"sudo -n tee /tmp/tlaloc-stage.tgz >/dev/null && "
              f"sudo -n tar -xzf /tmp/tlaloc-stage.tgz -C {INSTALL_DIR} && "
              f"sudo -n rm -f /tmp/tlaloc-stage.tgz")
    # text=False: this pushes a BINARY tar stream on stdin.
    proc = runner(ssh_argv(ssh_key, red_ip, remote),
                  input=tar.stdout, text=False, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"staging the agent failed: {_tail(proc)}")
    for name in ("teams.json", "boxes.json", "users.json", "box_services.json",
                 "box_vulns.json", "nakon-config.json", ".deploy_state.json",
                 "credentials.txt", "Compfile"):
        source = comp_dir / name
        if not source.exists():
            continue
        remote_put = f"sudo -n tee {INTEL_DIR}/{name} >/dev/null"
        proc = runner(ssh_argv(ssh_key, red_ip, remote_put),
                      input=source.read_bytes(), text=False, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"pushing {name} failed: {_tail(proc)}")
    # The seed re-reads the Compfile's red_yaml target from the intel dir; unstaged it fails.
    red_yaml = compfile_value(comp_dir, "red_yaml")
    if red_yaml:
        source = comp_dir / red_yaml
        if not source.is_file():
            raise RuntimeError(f"Compfile red_yaml {red_yaml!r} is missing from {comp_dir}")
        proc = runner(ssh_argv(ssh_key, red_ip,
                               f"sudo -n tee {INTEL_DIR}/{red_yaml} >/dev/null"),
                      input=source.read_bytes(), text=False, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"pushing {red_yaml} failed: {_tail(proc)}")
    # The planter reads payloads from <intel>/red/payloads/manifest.json; with no local
    # payload a channel cannot plant anything.
    payloads = comp_dir / "red" / "payloads"
    if (payloads / "manifest.json").exists():
        archive = subprocess.run(["tar", "-czf", "-", "-C", str(payloads.parent), "payloads"],
                                 capture_output=True)
        if archive.returncode != 0:
            raise RuntimeError("tar of the frozen payloads failed")
        remote = (f"sudo -n mkdir -p {INTEL_DIR}/red && "
                  f"sudo -n tee /tmp/tlaloc-payloads.tgz >/dev/null && "
                  f"sudo -n tar -xzf /tmp/tlaloc-payloads.tgz -C {INTEL_DIR}/red && "
                  f"sudo -n rm -f /tmp/tlaloc-payloads.tgz")
        proc = runner(ssh_argv(ssh_key, red_ip, remote), input=archive.stdout,
                      text=False, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"staging the frozen payloads failed: {_tail(proc)}")
    return {"ok": True}


def push_config(ssh_key, red_ip, config_text, *, runner=None, timeout=120):
    # tee reads stdin: a 0-byte config still "succeeds" and leaves red01 on tlaloc's defaults.
    remote = (f"sudo -n mkdir -p /etc/tlaloc && "
              f"sudo -n tee {CONFIG_PATH} >/dev/null && sudo -n chmod 600 {CONFIG_PATH}")
    proc = _ssh_run(ssh_key, red_ip, remote, runner=runner, timeout=timeout,
                    input=config_text)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(f"config push failed: {_tail(proc)}")
    return {"ok": True}


def push_llm_env(ssh_key, red_ip, api_key, *, runner=None, timeout=120):
    """The LLM key rides a separate root-owned env file the agent reads at start."""
    remote = (f"sudo mkdir -p /etc/tlaloc && printf 'TLALOC_LLM_API_KEY=%s\\n' "
              f"'{api_key}' | sudo tee /etc/tlaloc/env >/dev/null && "
              f"sudo chmod 600 /etc/tlaloc/env")
    proc = _ssh_run(ssh_key, red_ip, remote, runner=runner, timeout=timeout)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(f"pushing the LLM env file failed: {_tail(proc)}")
    return {"ok": True}


def install_unit(ssh_key, red_ip, *, runner=None, timeout=180):
    remote = (f"sudo -n tee /etc/systemd/system/{UNIT} < {INSTALL_DIR}/systemd/"
              f"{UNIT_SRC.name} >/dev/null && sudo -n systemctl daemon-reload && "
              f"sudo -n systemctl enable {UNIT}")
    proc = _ssh_run(ssh_key, red_ip, remote, runner=runner, timeout=timeout)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(f"installing {UNIT} failed: {_tail(proc)}")
    return {"ok": True}


def seed_via_red01(ssh_key, red_ip, *, runner=None, timeout=5400):
    proc = _ssh_run(ssh_key, red_ip, seed_command(), runner=runner, timeout=timeout)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(f"the day-0 seed failed on red01 {red_ip}: {_tail(proc)}")
    return {"ok": True}


def start_unit(ssh_key, red_ip, *, runner=None, timeout=60):
    """Start it after the seed: enable-only leaves the loop stopped until someone logs in."""
    remote = f"sudo -n systemctl start {UNIT} && sudo -n systemctl is-active {UNIT}"
    proc = _ssh_run(ssh_key, red_ip, remote, runner=runner, timeout=timeout)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(f"starting {UNIT} on {red_ip} failed: {_tail(proc)}")
    return {"ok": True}


def verify_config_contract(ssh_key, red_ip, *, intended=None, runner=None,
                           timeout=180):
    """Fail when the pushed config dropped a field the deploy meant to set."""
    proc = _ssh_run(ssh_key, red_ip, f"sudo -n cat {CONFIG_PATH}", runner=runner,
                    timeout=timeout)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(f"cannot read {CONFIG_PATH}: {_tail(proc)}")
    import yaml
    pushed = yaml.safe_load(getattr(proc, "stdout", "") or "") or {}
    wanted = _load_intended(intended)
    missing = [f for f in contract_fields(runner=runner)
               if _missing(pushed, f) and not _missing(wanted, f)]
    if missing:
        raise RuntimeError(f"the pushed config dropped {missing} — those features would "
                           f"run on defaults (or be dead) on the VM")
    return {"ok": True}


def _load_intended(intended):
    if intended is None:
        return {}
    if isinstance(intended, dict):
        return intended
    try:
        import yaml
        return yaml.safe_load(Path(intended).read_text()) or {}
    except Exception:
        return {}


def _missing(cfg, dotted):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return True
        node = node[part]
    return False


def _team_identifiers(team_keys):
    return [team["identifier"] for team in (team_keys or {}).values()
            if team.get("identifier") is not None]


def plant_assume_breach(ctx, *, runner=None, ssh_key=None, mythic=None,
                        mythic_runner=None, llm=None, llm_key=None):
    """[7/8] Deploy red01, install the C2, seed, prove the config — every step raises."""
    from infra.ownership import Ownership
    from red import gaia_ops, red01_ops, sliver_ops
    comp_dir = Path(getattr(ctx, "comp_dir"))
    # The workspace directory is not an identity: a dashboard run's comp dir is "comp" while the
    # competition is "scrim-one", so the same range named its state two ways and a seeded
    # .tlaloc-state from a manual run refused the deploy as cross-event state (live: scrim-one,
    # run-5882173c). tlaloc state is keyed by the competition's own name everywhere.
    comp_id = getattr(ctx, "comp_id", None) or competition_name(comp_dir)
    block = red_block(comp_dir)
    problems = validate_red_block(block)
    if problems:
        raise RuntimeError("the competition's red block has "
                           f"{len(problems)} problem(s): "
                           f"{', '.join(p['field'] for p in problems)}")
    ssh_key = ssh_key or getattr(ctx, "ssh_key_abs", None) or getattr(ctx, "ssh_key", None)
    if not ssh_key:
        raise RuntimeError("no ssh key on the deploy context — red01 cannot be reached")
    if not (TLALOC_REPO / "tlaloc" / "__init__.py").exists():
        raise RuntimeError(f"no tlaloc agent at {TLALOC_REPO}")

    ownership = Ownership(getattr(ctx, "comp_name", ""), getattr(ctx, "run_id", ""))
    run_id = getattr(ctx, "run_id", "")
    cfg_path = comp_dir / ".tlaloc-config.yaml"
    # A missing decision-model endpoint does not degrade: every decision silently falls back
    # while the run log looks healthy, so refuse the deploy rather than run a hollow red.
    from estate import config as estate_config
    clef_url = (block.get("clef_url")
                or (estate_config.active().knobs.clef_url or "").strip())
    if not clef_url:
        raise RuntimeError(
            "no decision-model endpoint: set the competition's red.clef_url or "
            "TEZ_CLEF_URL in the run host's environment (red01 must be able to "
            "reach it, or every decision silently falls back)")
    engine = str(block.get("c2_engine") or "sliver").lower()
    use_mythic = (bool(mythic) if mythic is not None
                  else engine == "mythic" and gaia_ops.mythic_enabled(comp_dir))
    use_sliver = (engine == "sliver" and not use_mythic
                  and sliver_ops.sliver_enabled(comp_dir))
    write_config(comp_dir, cfg_path,
                 comp_id=comp_id,
                 engine_ip=getattr(ctx, "scoring_ip", None) or getattr(ctx, "engine_ip", None),
                 clef_url=clef_url,
                 c2_url=block.get("c2_url") if use_mythic else None,
                 c2_operation=block.get("c2_operation"),
                 llm=llm)
    print("  red01      : provisioning (clone + network + engine rules)...")
    identity = red01_ops.deploy(
        comp_dir, engine_vmid=getattr(ctx, "engine_vmid", None),
        ssh_key=ssh_key, ownership=ownership,
        placement=getattr(ctx, "placement", None),
        node=getattr(ctx, "node", None),
        engine_ip=getattr(ctx, "scoring_ip", "") or "",
        red_bridge=red_bridge_name(run_id) if run_id else None)
    red_ip, red_vmid = identity["ip"], identity["vmid"]
    red01_ops.record_identity(comp_dir, getattr(ctx, "placement", None),
                              vmid=red_vmid, ip=red_ip, node=identity["node"])

    grown = resize_red_disk(comp_dir, red_ip, red_vmid, ssh_key=ssh_key, runner=runner)
    if not grown.get("ok"):
        raise RuntimeError(
            f"red01's disk could not grow to {red_disk_gb(comp_dir)}G — the C2 needs it "
            f"({str(grown.get('detail'))[:300]})")
    print(f"  c2          : red01 disk {grown.get('before_gb')}G -> "
          f"{grown.get('after_gb')}G (target {red_disk_gb(comp_dir)}G)")

    team_identifiers = _team_identifiers(getattr(ctx, "teams", None))
    callback_port = callback_host = callback_ports = None
    mythic_result = sliver_result = payload_manifest = None
    c2_summary = None
    if use_mythic:
        plan = gaia_ops.c2_plan(comp_dir, red_ip=red_ip, comp_id=comp_id)
        callback_port, callback_host = plan["callback_port"], plan["callback_host"]
        if not callback_port:
            raise RuntimeError(
                "the competition has no C2 callback port (`firewall_dnat <port>-><ip>` "
                "or `red.c2_callback_port` in its Compfile) — the frozen payloads would "
                "dial nothing and the channel could never check in")
        # Gaia's `user create` on an existing name prints a password that does not
        # authenticate, so a resume would freeze payloads as nobody: mint one per deploy.
        agent_user = f"tlaloc-{secrets.token_hex(3)}"
        mythic_result = gaia_ops.prepare_mythic(
            comp_dir, box_ip=plan["box"], ssh_user=RED_USER, ssh_key=ssh_key,
            runner=mythic_runner or runner, operation=plan["operation"],
            callback_host=plan["callback_host"],
            callback_port=plan["callback_port"], agent_user=agent_user)
        if not mythic_result.get("ok"):
            detail = "; ".join(mythic_result.get("degradations") or [])[:500]
            raise RuntimeError(f"the Mythic step failed — no C2 on red01 ({detail})")
        if mythic_result.get("c2"):
            gaia_ops.apply_c2_to_config(cfg_path, mythic_result["c2"])
        env = dict(os.environ)
        env.update(_env_pairs((mythic_result or {}).get("env") or ""))
        prep = (runner or _default_runner)(comp_prep_command(comp_dir),
                                           cwd=str(TLALOC_REPO), timeout=1800,
                                           env=env)
        if getattr(prep, "returncode", 1) != 0:
            raise RuntimeError(f"c2-prep (payload freeze) failed: {_tail(prep)}")
        c2_summary = {"engine": "mythic", "callback_port": callback_port,
                      "callback_host": callback_host}
    elif use_sliver:
        plan = sliver_ops.c2_plan(comp_dir, red_ip=red_ip, comp_id=comp_id)
        sliver_result = sliver_ops.prepare_sliver(
            comp_dir, box_ip=plan["box"], ssh_user=RED_USER, ssh_key=ssh_key,
            runner=runner, plan=plan)
        if not sliver_result.get("ok"):
            detail = "; ".join(sliver_result.get("degradations") or [])[:500]
            raise RuntimeError(f"the Sliver step failed — no C2 on red01 ({detail})")
        sliver_ops.apply_c2_to_config(cfg_path, sliver_result["c2"])
        callback_ports = plan["ports"]
        c2_summary = {"engine": "sliver", "transport_ports": callback_ports,
                      "domain": plan["domain"], "box": plan["box"]}

    # Without a C2 the DNAT is skipped but not the FORWARD trio: red01 must still reach teams.
    print("  red rules   : installing the red-segment rules on the engine...")
    red01_ops.install_engine_rules(
        getattr(ctx, "scoring_ip", "") or "", ssh_key,
        engine_user=getattr(ctx, "scoring_user", "sysadmin"),
        team_identifiers=team_identifiers,
        callback_port=callback_port, callback_host=callback_host or "",
        callback_ports=callback_ports, runner=runner)
    # Sliver builds its payloads ON red01 after the package is staged; Mythic's are
    # frozen on the operator before the box exists, so only that path stages them.
    stage_red01(ssh_key, red_ip, comp_dir, runner=runner, c2=bool(use_mythic))
    push_config(ssh_key, red_ip, cfg_path.read_text(), runner=runner)
    if mythic_result and mythic_result.get("env"):
        env_push = gaia_ops.push_env(ssh_key, red_ip, mythic_result["env"],
                                    runner=runner)
        if not env_push.get("ok"):
            raise RuntimeError(f"pushing the C2 env file failed: {env_push.get('detail')}")
    verify_config_contract(ssh_key, red_ip, intended=cfg_path, runner=runner)
    if use_sliver:
        if payloads_ready(ssh_key, red_ip, runner=runner):
            payload_manifest = pull_payload_manifest(ssh_key, red_ip, comp_dir,
                                                     runner=runner)
            print(f"  sliver      : reusing {payload_manifest['payloads']} frozen "
                  f"payload(s) already on red01")
        else:
            prep = _ssh_run(ssh_key, red_ip, comp_prep_remote_command("all"),
                            runner=runner, timeout=7200)
            if getattr(prep, "returncode", 1) != 0:
                raise RuntimeError(f"Sliver c2-prep (payload matrix) failed on red01: "
                                   f"{_tail(prep, 1500)}")
            payload_manifest = pull_payload_manifest(ssh_key, red_ip, comp_dir,
                                                     runner=runner)
            print(f"  sliver      : {payload_manifest['payloads']} frozen payload(s) "
                  f"across {len(callback_ports)} transport port(s)")
        c2_summary["payloads"] = payload_manifest["payloads"]
    if llm_key:
        push_llm_env(ssh_key, red_ip, llm_key, runner=runner)
    install_unit(ssh_key, red_ip, runner=runner)
    manifest = None
    if block.get("assume_breach") != "off":
        seed_via_red01(ssh_key, red_ip, runner=runner)
        manifest = pull_manifest(ssh_key, red_ip, comp_dir, runner=runner)
        seeded = _seeded_boxes(manifest["path"])
        if not seeded:
            raise RuntimeError(
                f"the day-0 seed left no planted box: the manifest at {manifest['path']} "
                f"lists none (tlaloc's seed exits 0 even when every box fails)")
        print(f"  tlaloc      : seed manifest proves {seeded} box(es) planted")
        if (use_mythic or use_sliver) and int(block.get("depth", 3)) >= 2:
            try:
                seeded_boxes = (json.loads(Path(manifest["path"]).read_text())
                                .get("boxes") or {})
            except (OSError, ValueError):
                seeded_boxes = {}
            no_channel = [ip for ip, rec in seeded_boxes.items()
                          if "channel" not in (rec.get("stages") or [])]
            if no_channel:
                raise RuntimeError(
                    f"the day-0 seed did not plant a checked-in C2 channel on "
                    f"{', '.join(no_channel)} — the payloads ran but their "
                    f"callbacks never landed (full spread proves every transport)")
    else:
        print("  tlaloc      : assume_breach off — no day-0 seed or manifest")
    start_unit(ssh_key, red_ip, runner=runner)
    print(f"  tlaloc      : loop started on red01 ({UNIT})")
    summary = {"engine": "tlaloc", "vmid": red_vmid, "red_ip": red_ip,
               "node": identity["node"], "config": str(cfg_path),
               "manifest": manifest.get("path") if manifest else None,
               "c2": c2_summary, "at": _now()}
    ctx.state["red"] = summary
    ctx.save_state()
    if manifest:
        print(f"  tlaloc      : seeded on red01 {red_ip} — the range is owned before T0 "
              f"(unit {UNIT}, state {STATE_DIR})")
    else:
        print(f"  tlaloc      : red01 ready on {red_ip} (assume_breach off — no seed; "
              f"unit {UNIT} started)")
    return summary

def destroy_tlaloc_red(comp_dir, *, ownership, engine_ip="", engine_user="sysadmin",
                       env=None, runner=None, ssh_key=None, red_ip=None):
    """Stop the unit with its verdict captured; only then destroy this run's red01."""
    from red import red01_ops
    runner = runner or _default_runner
    env = env if env is not None else os.environ
    identity = red01_ops.recorded_identity(comp_dir)
    ssh_key = ssh_key or env.get("TLALOC_SSH_KEY")
    red_ip = red_ip or identity.get("ip") or env.get("TLALOC_RED_IP")
    if ssh_key and red_ip:
        print(f"  red01      : capturing tlaloc's verdict on {red_ip}...")
        try:
            _ssh_run(ssh_key, red_ip, report_command(), runner=runner, timeout=300)
            pull_evidence(ssh_key, red_ip, Path(comp_dir) / "red", runner=runner)
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"  WARNING: red verdict capture failed ({type(exc).__name__}: {exc}) "
                  f"— the box still holds it")
        print(f"  red01      : stopping {UNIT} before teardown...")
        _ssh_run(ssh_key, red_ip, unit_stop_command(), runner=runner, timeout=180)
    from red import sliver_ops
    callback_ports = (sliver_ops.transport_ports()
                      if sliver_ops.sliver_enabled(comp_dir) else None)
    removed = red01_ops.destroy(
        comp_dir, ownership=ownership, vmid=identity.get("vmid"),
        node=identity.get("node"), engine_ip=engine_ip, ssh_key=ssh_key,
        engine_user=engine_user, runner=runner,
        callback_ports=callback_ports)
    return {"ok": True, "removed": removed}


VERDICT_FILES = ("restore-table.json", "report.md", "report-secrets.md", "world.json",
                 "events.jsonl")


def _seeded_boxes(manifest_path):
    try:
        data = json.loads(Path(manifest_path).read_text())
    except (OSError, ValueError):
        return 0
    return sum(1 for rec in (data.get("boxes") or {}).values()
               if (rec or {}).get("stages"))


def pull_manifest(ssh_key, red_ip, comp_dir, *, runner=None):
    dest = Path(comp_dir) / "red"
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / "manifest.json"
    proc = _ssh_run(ssh_key, red_ip, f"sudo -n cat {INTEL_DIR}/red/manifest.json",
                    runner=runner, timeout=120)
    if getattr(proc, "returncode", 1) == 0 and getattr(proc, "stdout", ""):
        target.write_text(proc.stdout)
        return {"path": str(target)}
    raise RuntimeError(f"the agent wrote no seed manifest at {INTEL_DIR}/red/manifest.json "
                       f"({_tail(proc)})")


def pull_evidence(ssh_key, red_ip, dest, *, runner=None, timeout=300):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    remote = f"sudo -n tar -czf - -C {STATE_DIR} ."
    proc = subprocess.run(ssh_argv(ssh_key, red_ip, remote),
                          capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"pulling red01's state failed: "
                           f"{(proc.stderr or b'').decode(errors='ignore')[-300:]}")
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        tar.extractall(dest, filter="data")
    manifest = Path(dest) / "manifest.json"
    if not manifest.exists():
        mproc = _ssh_run(ssh_key, red_ip, f"sudo -n cat {INTEL_DIR}/red/manifest.json",
                         runner=runner, timeout=120)
        if getattr(mproc, "returncode", 1) == 0 and getattr(mproc, "stdout", ""):
            manifest.write_text(mproc.stdout)
    write_verdict(dest)
    return {"dir": str(dest), "files": sorted(p.name for p in dest.iterdir())}


def collect_evidence(red_state_dir, evidence_dir, *, copy=None):
    import shutil
    copy = copy or shutil.copy2
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    pulled = {}
    for name in VERDICT_FILES + ("manifest.json",):
        source = Path(red_state_dir) / name
        if source.exists():
            target = evidence_dir / name
            copy(source, target)
            pulled[name] = str(target)
    return pulled


def red_verdict(evidence_dir, *, engine="tlaloc"):
    """The red verdict the dashboard consumes; coverage is the engine's job, not this."""
    evidence_dir = Path(evidence_dir)
    table_path = evidence_dir / "restore-table.json"
    verdict = {"engine": engine, "generated_at": _now(), "entries": 0,
               "commands": 0, "summary": {}, "restore_table": None,
               "report": None}
    if table_path.exists():
        try:
            table = json.loads(table_path.read_text())
        except ValueError:
            table = {}
        verdict["summary"] = table.get("summary") or {}
        verdict["entries"] = len(table.get("entries") or [])
        verdict["commands"] = len(table.get("commands") or [])
        verdict["restore_table"] = str(table_path)
    report = evidence_dir / "report.md"
    if report.exists():
        verdict["report"] = str(report)
    return verdict


def write_verdict(evidence_dir, *, engine="tlaloc", out=None):
    verdict = red_verdict(evidence_dir, engine=engine)
    target = Path(out) if out else Path(evidence_dir) / "verdict.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(verdict, indent=2))
    verdict["path"] = str(target)
    return verdict


def status(comp_dir):
    from red import red01_ops
    block = red_block(comp_dir)
    return {"engine": "tlaloc", "block": block,
            "problems": validate_red_block(block),
            "agent": str(TLALOC_REPO), "identity": red01_ops.recorded_identity(comp_dir)}


def _now():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _guest_grow_script():
    # sudo -n throughout: the ssh user is not root. The partition comes from pvs, not from
    # lsblk PKNAME of the root device — that is empty on an LV root.
    return (
        "set -x; ROOT=$(findmnt -no SOURCE /); "
        "PV=$(sudo -n pvs --noheadings -o pv_name 2>/dev/null | head -1 | tr -d ' '); "
        "if [ -n \"$PV\" ] && [ -b \"$PV\" ]; then "
        "  DISK=$(sudo -n lsblk -no PKNAME \"$PV\" 2>/dev/null | head -1); "
        "  NUM=$(cat \"/sys/class/block/$(basename \"$PV\")/partition\" 2>/dev/null); "
        "  if [ -n \"$DISK\" ] && [ -n \"$NUM\" ]; then "
        "    sudo -n growpart \"/dev/$DISK\" \"$NUM\" || true; "
        "  fi; "
        "  sudo -n pvresize \"$PV\" || true; "
        "fi; "
        "sudo -n lvextend -l +100%FREE \"$ROOT\" || true; "
        "sudo -n resize2fs \"$ROOT\" || true; df -h /")


def resize_red_disk(comp_dir, red_ip, vmid, *, ssh_key, target_gb=None,
                    runner=None, timeout=600):
    from infra.client import proxmox_api, wait_for_proxmox_task
    runner = runner or _default_runner
    target = int(target_gb or red_disk_gb(comp_dir))
    node = None
    for vm in ((proxmox_api("GET", "/cluster/resources", params={"type": "vm"}) or {})
               .get("data") or []):
        if vm.get("vmid") == vmid:
            node = vm.get("node")
            break
    if not node:
        raise RuntimeError(f"vmid {vmid} not found in the cluster")
    config = (proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config") or {}).get("data") or {}
    disk = next((key for key in ("scsi0", "virtio0", "sata0") if config.get(key)), None)
    if not disk:
        raise RuntimeError(f"vmid {vmid} has no scsi0/virtio0/sata0 disk")
    current_gb = _disk_gb(config.get(disk))
    if not current_gb or current_gb < target:
        # The API wants an absolute size, and the guest grow must wait for PVE's resize task
        # or growpart still sees the old disk.
        res = proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/resize",
                          data={"disk": disk, "size": f"{target}G"})
        upid = (res or {}).get("data") if isinstance(res, dict) else None
        if isinstance(upid, str) and upid.startswith("UPID:"):
            wait_for_proxmox_task(node, upid, timeout=300)
    proc = runner(["ssh", "-i", str(ssh_key), "-o", "StrictHostKeyChecking=no",
                   "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
                   f"{RED_USER}@{red_ip}", _guest_grow_script()], timeout=timeout)
    after = None
    for line in (getattr(proc, "stdout", "") or "").splitlines():
        line = line.strip()
        if line.startswith("/dev/") and "%" in line:
            after = _gb_from_df(line)
    ok = after is not None and after >= min(target - 2, target * 0.9)
    return {"ok": bool(ok), "disk": disk, "before_gb": current_gb, "after_gb": after,
            "grew": bool(not current_gb or current_gb < target),
            "detail": _tail(proc, 400)}


def _disk_gb(value):
    import re
    match = re.search(r"size=(\d+(?:\.\d+)?)([GMT])", str(value or ""))
    if not match:
        return None
    number = float(match.group(1))
    return int(number * {"G": 1, "M": 1 / 1024, "T": 1024}[match.group(2)])


def _gb_from_df(line):
    import re
    match = re.search(r"\s(\d+(?:\.\d+)?)([GMT])\s", str(line))
    if not match:
        return None
    number = float(match.group(1))
    return int(number * {"G": 1, "M": 1 / 1024, "T": 1024}[match.group(2)])
