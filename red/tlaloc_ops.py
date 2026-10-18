"""tlaloc as the range's red team — deploy, seed, verdict, destroy (phase 7).

What this module owns:

* the competition's ``red.*`` block (parse, validate, render into
  ``/etc/tlaloc/config.yaml`` — the file red01 actually reads);
* red01 itself, through ``red.red01_ops``: clone, network, engine rules, destroy.
  Red is a range component owned by this tree, not a sibling checkout;
* staging the agent onto red01 (``/opt/tlaloc`` — staged, never installed: the
  systemd unit is what puts it on the import path), the config contract, the C2
  payload freeze and the day-0 seed;
* the after-action verdict: ``tlaloc report`` is run on the box and every state
  artifact is pulled into ``<comp>/red/`` before the VM is destroyed.

Every failure here is a deploy failure. A range with no red team is not a range.
"""

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

from common.constants import red_bridge_name
from common.utils import local_python

REPO = Path(__file__).resolve().parents[1]
TLALOC_REPO = REPO / "tlaloc"

RED_USER = "sysadmin"
INSTALL_DIR = "/opt/tlaloc"
CONFIG_PATH = "/etc/tlaloc/config.yaml"
STATE_DIR = "/var/lib/tlaloc"
INTEL_DIR = "/var/lib/tlaloc/intel"
UNIT = "tlaloc.service"
UNIT_SRC = TLALOC_REPO / "systemd" / "tlaloc.service"
# red01 hosts Mythic, and a beacon-sized disk cannot: the C2's images filled a 9.8G
# disk, Docker never started and the channel had nowhere to call home.
DEFAULT_RED_DISK_GB = 60

# Keys that belong in tlaloc's own config vs. keys the engine consumes.
CONFIG_KEYS = ("assume_breach", "depth", "duration_min", "ladder", "caps",
               "windows", "budget")
TAKEDOWN_MODES = ("stop", "stop_mask", "stop_rename", "stop_break_config",
                  "stop_rename_config", "block_port", "recon")


# --------------------------------------------------------------------------- #
# the Compfile red block
# --------------------------------------------------------------------------- #

def compfile_lines(comp_dir):
    """{key: value} for every `key value` line in the Compfile (last wins)."""
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
    """The competition's red.* block as a dict (yaml pointer over dotted keys)."""
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
    """Per-field problems for a competition's red block ([] means deployable)."""
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
    return problems


def tlaloc_config(comp_dir, block=None, *, comp_id=None, engine_ip=None,
                  red_ip=None, quotient_url=None, clef_url=None, c2_url=None,
                  c2_operation=None, llm=None):
    """The body of /etc/tlaloc/config.yaml for this competition.

    Only values the deploy knows are written: everything else stays at tlaloc's
    documented defaults, so the pushed file is a diff against DEFAULTS and
    `tlaloc verify-config` can diff it back per field.
    """
    block = block if block is not None else red_block(comp_dir)
    comp_dir = Path(comp_dir).resolve()
    cfg = {
        "run": {"comp": comp_id or comp_dir.name,
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
        cfg.setdefault("clef", {})["base_url"] = clef_url
    if c2_url:
        cfg.setdefault("c2", {}).update({"enabled": True, "base_url": c2_url,
                                         "operation": c2_operation or cfg["run"]["comp"]})
    if llm:
        cfg["llm"] = dict(llm)
    return cfg


def write_config(comp_dir, path=None, **kwargs):
    """Render + write the competition's tlaloc config; returns the path."""
    import yaml
    target = Path(path) if path else Path(comp_dir) / ".tlaloc-config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(tlaloc_config(comp_dir, **kwargs),
                                     sort_keys=False))
    target.chmod(0o600)
    return target


def contract_fields(runner=None):
    """The fields red01 reads, straight from tlaloc (`verify-config --print-contract`)."""
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


# --------------------------------------------------------------------------- #
# command builders (pure — the tests assert these, the runner executes them)
# --------------------------------------------------------------------------- #

def _default_runner(cmd, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


def ssh_argv(ssh_key, red_ip, remote, timeout=30):
    return ["ssh", "-i", str(ssh_key), "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={timeout}", f"{RED_USER}@{red_ip}", remote]


def stage_command(comp_dir):
    """Stage the package + intel on red01 (staged, never installed)."""
    comp_dir = Path(comp_dir).resolve()
    return (
        f"sudo -n mkdir -p {INSTALL_DIR} {INTEL_DIR} {STATE_DIR} && "
        f"sudo -n chown -R {RED_USER}: {STATE_DIR} {INTEL_DIR} && "
        f"sudo -n tar -xzf - -C {INSTALL_DIR}")


def seed_command():
    """The day-0 seed, run ON red01 from the install dir.

    `cd /opt/tlaloc` is not cosmetic: the package is staged, so `python3 -m
    tlaloc` from the login dir dies with "No module named tlaloc". `--comp` is the
    pushed intel dir: without it the agent loads blind intel (no boxes, nothing to
    seed) while the deploy still reports a successful seed.
    """
    return (f"cd {INSTALL_DIR} && sudo -n python3 -m tlaloc "
            f"--config {CONFIG_PATH} --comp {INTEL_DIR} seed")


def plant_command():
    return (f"cd {INSTALL_DIR} && sudo -n python3 -m tlaloc "
            f"--config {CONFIG_PATH} --comp {INTEL_DIR} run")


def report_command():
    """Write the after-action report + restore table on the box (idempotent)."""
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


def _operator_env(comp_dir, mythic_result):
    """The Mythic operator credentials c2-prep authenticates with.

    Gaia mints the operator password and prints it once. The first real mint is
    kept 0600 under ~/.tezcatlipoca (outside every repo) so a resume, or a manual
    c2-prep after a crash, can still log in.
    """
    body = (mythic_result or {}).get("env") or ""
    fresh = _env_pairs(body)
    admin = (mythic_result or {}).get("admin_password")
    password = fresh.get("TLALOC_MYTHIC_PASSWORD")
    store = Path.home() / ".tezcatlipoca" / "c2" / f"{Path(comp_dir).name}.env"
    if password and password != admin:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(body.strip() + "\n", encoding="utf-8")
        store.chmod(0o600)
        return fresh
    if store.exists():
        return _env_pairs(store.read_text(encoding="utf-8"))
    return fresh


def comp_prep_command(comp_dir, transports=("http", "webhook")):
    comp_dir = Path(comp_dir).resolve()
    # --state-dir: c2-prep runs on the OPERATOR, where the config's VM path
    # (/var/lib/tlaloc) is unwritable; build_context mkdirs it.
    return [local_python(), "-m", "tlaloc", "--comp", str(comp_dir),
            "--config", str(comp_dir / ".tlaloc-config.yaml"),
            "--state-dir", str(comp_dir / ".tlaloc-state"), "c2-prep",
            "--transports", ",".join(transports)]


def unit_stop_command():
    return f"sudo -n systemctl disable --now {UNIT} || true"


# --------------------------------------------------------------------------- #
# the phase-7 entry point
# --------------------------------------------------------------------------- #

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


def stage_red01(ssh_key, red_ip, comp_dir, *, runner=None, timeout=600):
    """Push /opt/tlaloc + the unit file to red01, then its nakon intel."""
    comp_dir = Path(comp_dir).resolve()
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
    return {"ok": True}


def push_config(ssh_key, red_ip, config_text, *, runner=None, timeout=120):
    # `tee` reads stdin: without the rendered config every deploy wrote a 0-byte file
    # and red01 ran on tlaloc's defaults while the contract check named every field.
    remote = (f"sudo -n mkdir -p /etc/tlaloc && "
              f"sudo -n tee {CONFIG_PATH} >/dev/null && sudo -n chmod 600 {CONFIG_PATH}")
    proc = _ssh_run(ssh_key, red_ip, remote, runner=runner, timeout=timeout,
                    input=config_text)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(f"config push failed: {_tail(proc)}")
    return {"ok": True}


def push_llm_env(ssh_key, red_ip, api_key, *, runner=None, timeout=120):
    """The LLM credential rides a 0600 root-owned env file the agent reads at start."""
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


def verify_config_contract(ssh_key, red_ip, *, intended=None, runner=None,
                           timeout=180):
    """Fetch the pushed config back and diff it against what red01 must read.

    The pushed file is a DIFF against tlaloc's documented defaults, so a field
    absent from it is not a defect by itself. A defect is a field the deploy MEANT
    to set that the pushed file does not carry — that field's feature is dead on
    the VM while every local test stays green.
    """
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
    """The config the deploy meant to push (a dict, a path, or None -> only defaults)."""
    if intended is None:
        return {}
    if isinstance(intended, dict):
        return intended
    try:
        import yaml
        return yaml.safe_load(Path(intended).read_text()) or {}
    except Exception:                                  # unreadable -> defaults only
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
    """[7/8] Deploy red01, install the C2, seed the agent, prove the config.

    Every step raises on failure. A competition with no working red team is a
    failed deploy, not a degraded range.
    """
    from infra.ownership import Ownership
    from red import gaia_ops, red01_ops
    comp_dir = Path(getattr(ctx, "comp_dir"))
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
    write_config(comp_dir, cfg_path,
                 comp_id=getattr(ctx, "comp_id", None) or comp_dir.name,
                 engine_ip=getattr(ctx, "scoring_ip", None) or getattr(ctx, "engine_ip", None),
                 clef_url=block.get("clef_url"),
                 c2_url=block.get("c2_url"),
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
            f"red01's disk could not grow to {red_disk_gb(comp_dir)}G — Mythic needs it "
            f"({str(grown.get('detail'))[:300]})")
    print(f"  mythic      : red01 disk {grown.get('before_gb')}G -> "
          f"{grown.get('after_gb')}G (target {red_disk_gb(comp_dir)}G)")

    team_identifiers = _team_identifiers(getattr(ctx, "teams", None))
    callback_port = callback_host = None
    mythic_result = None
    if mythic is None:
        mythic = gaia_ops.mythic_enabled(comp_dir)
    if mythic:
        plan = gaia_ops.c2_plan(comp_dir, red_ip=red_ip,
                                comp_id=getattr(ctx, "comp_id", None))
        callback_port, callback_host = plan["callback_port"], plan["callback_host"]
        if not callback_port:
            raise RuntimeError(
                "the competition has no C2 callback port (`firewall_dnat <port>-><ip>` "
                "or `red.c2_callback_port` in its Compfile) — the frozen payloads would "
                "dial nothing and the channel could never check in")
        mythic_result = gaia_ops.prepare_mythic(
            comp_dir, box_ip=plan["box"], ssh_user=RED_USER, ssh_key=ssh_key,
            runner=mythic_runner or runner, operation=plan["operation"],
            callback_host=plan["callback_host"],
            callback_port=plan["callback_port"])
        if not mythic_result.get("ok"):
            detail = "; ".join(mythic_result.get("degradations") or [])[:500]
            raise RuntimeError(f"the Mythic step failed — no C2 on red01 ({detail})")
        if mythic_result.get("c2"):
            gaia_ops.apply_c2_to_config(cfg_path, mythic_result["c2"])
        # c2-prep runs on the operator and must authenticate as the competition's
        # Mythic operator; Gaia prints that password once and never again, so a
        # resume has to read the copy kept from the run that minted it.
        env = dict(os.environ)
        env.update(_operator_env(comp_dir, mythic_result))
        prep = (runner or _default_runner)(comp_prep_command(comp_dir),
                                           cwd=str(TLALOC_REPO), timeout=1800,
                                           env=env)
        if getattr(prep, "returncode", 1) != 0:
            raise RuntimeError(f"c2-prep (payload freeze) failed: {_tail(prep)}")

    # The segment rules are not optional: red01 must be able to reach the teams even
    # when the competition runs without a C2 (the DNAT is skipped, the FORWARD trio is not).
    print("  red rules   : installing the red-segment rules on the engine...")
    red01_ops.install_engine_rules(
        getattr(ctx, "scoring_ip", "") or "", ssh_key,
        engine_user=getattr(ctx, "scoring_user", "sysadmin"),
        team_identifiers=team_identifiers,
        callback_port=callback_port, callback_host=callback_host or "",
        runner=runner)
    stage_red01(ssh_key, red_ip, comp_dir, runner=runner)
    push_config(ssh_key, red_ip, cfg_path.read_text(), runner=runner)
    if mythic_result and mythic_result.get("env"):
        env_push = gaia_ops.push_env(ssh_key, red_ip, mythic_result["env"],
                                    runner=runner)
        if not env_push.get("ok"):
            raise RuntimeError(f"pushing the C2 env file failed: {env_push.get('detail')}")
    verify_config_contract(ssh_key, red_ip, intended=cfg_path, runner=runner)
    if llm_key:
        push_llm_env(ssh_key, red_ip, llm_key, runner=runner)
    install_unit(ssh_key, red_ip, runner=runner)
    seed_via_red01(ssh_key, red_ip, runner=runner)
    manifest = pull_manifest(ssh_key, red_ip, comp_dir, runner=runner)
    seeded = _seeded_boxes(manifest["path"])
    if not seeded:
        raise RuntimeError(
            f"the day-0 seed left no planted box: the manifest at {manifest['path']} "
            f"lists none (tlaloc's seed exits 0 even when every box fails)")
    print(f"  tlaloc      : seed manifest proves {seeded} box(es) planted")
    summary = {"engine": "tlaloc", "vmid": red_vmid, "red_ip": red_ip,
               "node": identity["node"], "config": str(cfg_path),
               "manifest": manifest.get("path"), "at": _now()}
    ctx.state["red"] = summary
    ctx.save_state()
    print(f"  tlaloc      : seeded on red01 {red_ip} — the range is owned before T0 "
          f"(unit {UNIT}, state {STATE_DIR})")
    return summary


def destroy_tlaloc_red(comp_dir, *, ownership, engine_ip="", engine_user="sysadmin",
                       env=None, runner=None, ssh_key=None, red_ip=None):
    """Capture red's verdict, stop the unit, then remove exactly this run's red01.

    Order matters: the report + restore table must be generated and pulled while
    the box still exists, and the unit must stop before the VM disappears.
    """
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
    removed = red01_ops.destroy(
        comp_dir, ownership=ownership, vmid=identity.get("vmid"),
        node=identity.get("node"), engine_ip=engine_ip, ssh_key=ssh_key,
        engine_user=engine_user, runner=runner)
    return {"ok": True, "removed": removed}


# --------------------------------------------------------------------------- #
# verdict + evidence
# --------------------------------------------------------------------------- #

VERDICT_FILES = ("restore-table.json", "report.md", "report-secrets.md", "world.json",
                 "events.jsonl")


def _seeded_boxes(manifest_path):
    """Count boxes the seed manifest says were planted (a stage landed on them)."""
    try:
        data = json.loads(Path(manifest_path).read_text())
    except (OSError, ValueError):
        return 0
    return sum(1 for rec in (data.get("boxes") or {}).values()
               if (rec or {}).get("stages"))


def pull_manifest(ssh_key, red_ip, comp_dir, *, runner=None):
    """Fetch the seed manifest the agent wrote into its intel dir."""
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
    """Tar the agent's whole state dir off red01 into `dest` (before destroy)."""
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
    """Copy already-pulled verdict files into an evidence tree."""
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
    """The machine-readable red verdict the dashboard consumes.

    `coverage` is the ENGINE's job (it reads the scoreboard); this assembles what
    only red knows: what was planted, what was taken down, how to undo it.
    """
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
    """What the engine knows about this competition's red setup."""
    from red import red01_ops
    block = red_block(comp_dir)
    return {"engine": "tlaloc", "block": block,
            "problems": validate_red_block(block),
            "agent": str(TLALOC_REPO), "identity": red01_ops.recorded_identity(comp_dir)}


def _now():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _guest_grow_script():
    """Extend the root filesystem into whatever space the hypervisor just added.

    Every write needs `sudo -n` (red01's ssh user is not root), and the partition
    is found through LVM (`pvs`), not by walking up from the root device: on an LV
    root `lsblk -no PKNAME /dev/dm-N` is empty.
    """
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
    """Grow red01's disk to the Mythic floor (hypervisor resize + guest grow)."""
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
        # PUT with an ABSOLUTE size: the API rejects a relative size, and the guest
        # grow must wait for PVE's resize task or growpart still sees the old disk.
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
    """`scsi0: vmdata:vm-2099-disk-0,size=10G` -> 10."""
    import re
    match = re.search(r"size=(\d+(?:\.\d+)?)([GMT])", str(value or ""))
    if not match:
        return None
    number = float(match.group(1))
    return int(number * {"G": 1, "M": 1 / 1024, "T": 1024}[match.group(2)])


def _gb_from_df(line):
    """A `df -h /` row -> the size in GB (e.g. '59G')."""
    import re
    match = re.search(r"\s(\d+(?:\.\d+)?)([GMT])\s", str(line))
    if not match:
        return None
    number = float(match.group(1))
    return int(number * {"G": 1, "M": 1 / 1024, "T": 1024}[match.group(2)])
