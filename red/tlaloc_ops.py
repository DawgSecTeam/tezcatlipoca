"""tlaloc v2 as the range's red team — deploy, seed, verdict, destroy (phase 7).

Selected by the Compfile knob ``red.engine tlaloc``. The default is **badauto**
(v1): the migration plan keeps v1 as the production red team until v2 passes a
scrim, so a competition that says nothing is unchanged.

What this module owns:

* the competition's ``red.*`` block (parse, validate, and render into
  ``/etc/tlaloc/config.yaml`` — the file red01 actually reads);
* the assumption that the red VM and the engine DNAT are provisioned by the
  EXISTING path (``red_plant_ops._deploy_red`` / badauto's engine_nat). That is
  deliberate for this phase: one copy of the provisioning logic, and a tlaloc
  scrim needs no new Proxmox/NAT code. The engine owning red01 outright is the
  migration plan's phase-4 exit gate, not a prerequisite for a first run;
* staging the tlaloc package onto red01 (``/opt/tlaloc`` — staged, never
  pip-installed: the systemd unit is what puts it on the import path), pushing
  the config + the competition's intel, installing ``tlaloc.service``, disabling
  ``bad-auto.service`` and running the day-0 ``tlaloc seed``;
* comp-prep (the `tlaloc c2-prep` payload freeze) BEFORE T0;
* the after-action verdict artifact: tlaloc's own ``restore-table.json`` +
  ``report.md`` pulled from red01's state dir into ``evidence/red/``.

Everything is warn-and-continue and recorded as a degradation: a red problem must
never abort a deploy whose range is otherwise healthy. Teardown must still run
the VM's destroy path (``destroy_tlaloc_red``) or red01 survives with its key,
its config and its DNAT rules.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from common.utils import local_python, record_degradation

REPO = Path(__file__).resolve().parents[1]
TLALOC_REPO = REPO.parent / "tlaloc"

RED_USER = "sysadmin"
INSTALL_DIR = "/opt/tlaloc"
CONFIG_PATH = "/etc/tlaloc/config.yaml"
STATE_DIR = "/var/lib/tlaloc"
INTEL_DIR = "/var/lib/tlaloc/intel"
UNIT = "tlaloc.service"
UNIT_SRC = TLALOC_REPO / "systemd" / "tlaloc.service"

ENGINE_KEY = "red.engine"
# v1 first: a competition that says nothing keeps running the engine that has.
ENGINES = ("badauto", "tlaloc")
DEFAULT_ENGINE = "badauto"

# The knobs this module understands in a Compfile (dotted `red.` keys, the
# `red_yaml` pointer, and the legacy v1 names so existing competitions keep
# working). Anything else is passed through untouched.
LEGACY_ASSUME = {"0": "off", "1": "light"}
# Keys that belong in tlaloc's own config vs. keys the ENGINE consumes.
CONFIG_KEYS = ("assume_breach", "depth", "duration_min", "ladder", "caps",
               "windows", "budget")
ENGINE_KEYS = ("engine", "red_ip")
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
    """The competition's red.* block as a dict (yaml pointer + legacy knobs)."""
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
    if "assume_breach" in lines and "assume_breach" not in block:
        block["assume_breach"] = LEGACY_ASSUME.get(lines["assume_breach"],
                                                  lines["assume_breach"])
    if "assume_breach_depth" in lines and "depth" not in block:
        try:
            block["depth"] = int(lines["assume_breach_depth"])
        except ValueError:
            pass
    return block


def red_engine(comp_dir):
    """Which red engine this competition deploys (default: the v1 path)."""
    block = red_block(comp_dir)
    engine = str(compfile_lines(comp_dir).get(ENGINE_KEY)
                 or block.get("engine") or DEFAULT_ENGINE).strip().lower()
    return engine if engine in ENGINES else DEFAULT_ENGINE


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
                  c2_operation=None):
    """The body of /etc/tlaloc/config.yaml for this competition.

    Only values the deploy knows are written: everything else stays at tlaloc's
    documented defaults, so the pushed file is a diff against DEFAULTS and
    `tlaloc verify-config` can diff it back per field.
    """
    block = block if block is not None else red_block(comp_dir)
    comp_dir = Path(comp_dir).resolve()
    cfg = {
        "run": {"comp": comp_id or comp_dir.name,
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
    return cfg


def write_config(comp_dir, path=None, **kwargs):
    """Render + write the competition's tlaloc config; returns the path."""
    import yaml
    target = Path(path) if path else Path(comp_dir) / ".tlaloc-config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(tlaloc_config(comp_dir, **kwargs),
                                     sort_keys=False))
    return target


def contract_fields(runner=None):
    """The fields red01 reads, straight from tlaloc (`verify-config --print-contract`).

    One definition, in the repo that owns the code — a copy here would drift and
    a field the push omits is a feature that is dead on the VM while every local
    test stays green.
    """
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
        f"sudo -n chown -R {RED_USER}: {STATE_DIR} && "
        f"sudo -n tar -xzf - -C {INSTALL_DIR} && "
        f"cd {INSTALL_DIR} && sudo -n systemctl disable --now bad-auto.service "
        f"2>/dev/null || true")


def seed_command(comp_dir=None):
    """The day-0 seed, run ON red01 from the install dir.

    `cd /opt/tlaloc` is not cosmetic: the package is staged, so `python3 -m
    tlaloc` from the login dir dies with "No module named tlaloc" and the whole
    pre-T0 seed is lost (v1's badge, one module later).
    """
    # No --depth: the CLI seeds at the depth the pushed config (and therefore the
    # competition's Compfile) declares, so the config stays the single source.
    return (f"cd {INSTALL_DIR} && sudo -n python3 -m tlaloc "
            f"--config {CONFIG_PATH} seed")


def plant_command():
    return f"cd {INSTALL_DIR} && sudo -n python3 -m tlaloc --config {CONFIG_PATH} run"


def unit_install_command(local_unit=None):
    unit = Path(local_unit) if local_unit else UNIT_SRC
    return (f"sudo -n tee /etc/systemd/system/{UNIT} < {INSTALL_DIR}/systemd/"
            f"{unit.name} >/dev/null && sudo -n systemctl daemon-reload && "
            f"sudo -n systemctl enable --now {UNIT}")


def comp_prep_command(comp_dir, transports=("http", "webhook")):
    comp_dir = Path(comp_dir).resolve()
    return [local_python(), "-m", "tlaloc", "--comp", str(comp_dir),
            "--config", str(comp_dir / ".tlaloc-config.yaml"), "c2-prep",
            "--transports", ",".join(transports)]


def destroy_command(comp_dir):
    """The VM + NAT teardown still belongs to the v1 provisioning path."""
    return [local_python(), "-m", "badauto", "destroy", "--competition",
            str(Path(comp_dir).resolve()), "--yes"]


def unit_stop_command():
    return f"sudo -n systemctl disable --now {UNIT} || true"


# --------------------------------------------------------------------------- #
# the phase-7 entry point
# --------------------------------------------------------------------------- #

def _ssh_run(ssh_key, red_ip, remote, *, runner=None, timeout=900):
    runner = runner or _default_runner
    try:
        return runner(ssh_argv(ssh_key, red_ip, remote), timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([], 124, "", f"timeout after {timeout}s")


def _tail(proc, n=300):
    return (((getattr(proc, "stderr", "") or "") + (getattr(proc, "stdout", "") or ""))[-n:])


def stage_red01(ssh_key, red_ip, comp_dir, *, runner=None, timeout=600):
    """Push /opt/tlaloc + the unit file to an already-provisioned red01."""
    comp_dir = Path(comp_dir).resolve()
    if not (TLALOC_REPO / "tlaloc" / "__init__.py").exists():
        return {"ok": False, "reason": f"no tlaloc checkout at {TLALOC_REPO}"}
    if not UNIT_SRC.exists():
        return {"ok": False, "reason": f"no systemd unit at {UNIT_SRC}"}
    tar = subprocess.run(["tar", "-czf", "-", "-C", str(TLALOC_REPO),
                          "tlaloc", "systemd", "config.example.yaml"],
                         capture_output=True)
    if tar.returncode != 0:
        return {"ok": False, "reason": "tar of the tlaloc checkout failed"}
    runner = runner or _default_runner
    remote = (f"sudo -n mkdir -p {INSTALL_DIR} {INTEL_DIR} {STATE_DIR} && "
              f"sudo -n tee /tmp/tlaloc-stage.tgz >/dev/null && "
              f"sudo -n tar -xzf /tmp/tlaloc-stage.tgz -C {INSTALL_DIR}")
    proc = runner(["ssh", "-i", str(ssh_key), "-o", "StrictHostKeyChecking=no",
                   "-o", "UserKnownHostsFile=/dev/null",
                   f"{RED_USER}@{red_ip}", remote],
                  input=tar.stdout, timeout=timeout)
    if proc.returncode != 0:
        return {"ok": False, "reason": _tail(proc)}
    for name in ("teams.json", "boxes.json", "users.json", "box_services.json",
                 "box_vulns.json", "nakon-config.json", ".deploy_state.json",
                 "credentials.txt"):
        source = comp_dir / name
        if not source.exists():
            continue
        remote_put = (f"sudo -n tee {INTEL_DIR}/{name} >/dev/null")
        with open(source, "rb") as fh:
            put = runner(["ssh", "-i", str(ssh_key), "-o", "StrictHostKeyChecking=no",
                          "-o", "UserKnownHostsFile=/dev/null",
                          f"{RED_USER}@{red_ip}", remote_put],
                         input=fh.read(), timeout=timeout)
        if put.returncode != 0:
            return {"ok": False, "reason": f"pushing {name}: {_tail(put)}"}
    return {"ok": True}


def push_config(ssh_key, red_ip, config_text, *, runner=None, timeout=120):
    remote = (f"sudo -n mkdir -p /etc/tlaloc && "
              f"sudo -n tee {CONFIG_PATH} >/dev/null && sudo -n chmod 600 {CONFIG_PATH}")
    proc = _ssh_run(ssh_key, red_ip, remote, runner=runner, timeout=timeout)
    return {"ok": getattr(proc, "returncode", 1) == 0, "detail": _tail(proc)}


def install_unit(ssh_key, red_ip, *, runner=None, timeout=180):
    remote = (f"sudo -n tee /etc/systemd/system/{UNIT} < {INSTALL_DIR}/systemd/"
              f"{UNIT_SRC.name} >/dev/null && sudo -n systemctl daemon-reload && "
              f"sudo -n systemctl enable {UNIT}")
    proc = _ssh_run(ssh_key, red_ip, remote, runner=runner, timeout=timeout)
    return {"ok": getattr(proc, "returncode", 1) == 0, "detail": _tail(proc)}


def seed_via_red01(ssh_key, red_ip, *, runner=None, timeout=5400):
    proc = _ssh_run(ssh_key, red_ip, seed_command(), runner=runner, timeout=timeout)
    return {"ok": getattr(proc, "returncode", 1) == 0, "detail": _tail(proc)}


def verify_config_contract(ssh_key, red_ip, *, intended=None, runner=None,
                           timeout=180):
    """Fetch the pushed config back and diff it against what red01 must read.

    The pushed file is a DIFF against tlaloc's documented defaults (the renderer
    writes only what the deploy knows), so a field absent from it is not a defect
    by itself — red01 falls back to the default. A defect is a field the deploy
    MEANT to set (it is in `intended`) that the pushed file does not carry.

    The v1 lesson this exists for: a push allowlist that omits a field the red VM
    reads makes the feature dead on the VM while every local test stays green
    (the multi-transport beacon shipped that way and planted one legacy channel
    per box).
    """
    proc = _ssh_run(ssh_key, red_ip, f"sudo -n cat {CONFIG_PATH}", runner=runner,
                    timeout=timeout)
    if getattr(proc, "returncode", 1) != 0:
        return {"ok": False, "reason": f"cannot read {CONFIG_PATH}: {_tail(proc)}"}
    import yaml
    pushed = yaml.safe_load(getattr(proc, "stdout", "") or "") or {}
    wanted = _load_intended(intended)
    missing = [f for f in contract_fields(runner=runner)
               if _missing(pushed, f) and not _missing(wanted, f)]
    return {"ok": not missing, "missing": missing}


def _load_intended(intended):
    """The config the deploy meant to push (a dict, a path to the rendered file,
    or None -> {} meaning "only tlaloc's defaults are expected")."""
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


def plant_assume_breach(ctx, *, runner=None, ssh_key=None, red_ip=None,
                        provision=None):
    """[7/8] Deploy red01 (v1 provisioning) and seed tlaloc's assume-breach presence.

    Warn-and-continue: every failure is a degradation, never an aborted deploy.
    """
    comp_dir = Path(getattr(ctx, "comp_dir"))
    block = red_block(comp_dir)
    problems = validate_red_block(block)
    if problems:
        record_degradation("tlaloc_red_config", json.dumps(problems)[:400])
        print(f"  tlaloc: REFUSING — the competition's red block has "
              f"{len(problems)} problem(s): "
              f"{', '.join(p['field'] for p in problems)}")
        return {"ok": False, "reason": "invalid red block", "problems": problems}
    ssh_key = ssh_key or getattr(ctx, "ssh_key_abs", None) or getattr(ctx, "ssh_key", None)
    if not ssh_key:
        record_degradation("tlaloc_red", "no ssh key on the deploy context")
        return {"ok": False, "reason": "no ssh key"}
    if not (TLALOC_REPO / "tlaloc" / "__init__.py").exists():
        record_degradation("tlaloc_red", f"no tlaloc checkout at {TLALOC_REPO}")
        print(f"  tlaloc: no checkout at {TLALOC_REPO} — skipped")
        return {"ok": False, "reason": "no tlaloc checkout"}

    # 0. render the competition's tlaloc config FIRST: c2-prep, the push and the
    #    seed all read it, and a missing explicit --config is a hard error in
    #    tlaloc (running on silent defaults is how a deploy ended up on the wrong
    #    VM). Operator-supplied endpoints come from the Compfile: `red.clef_url`,
    #    `red.c2_url` (and `red.c2_operation`, default = the competition id).
    cfg_path = Path(comp_dir) / ".tlaloc-config.yaml"
    if not cfg_path.exists():
        lines = compfile_lines(comp_dir)
        write_config(comp_dir, cfg_path, comp_id=getattr(ctx, "comp_id", None) or comp_dir.name,
                     engine_ip=getattr(ctx, "engine_ip", None),
                     clef_url=block.get("clef_url") or lines.get("red.clef_url"),
                     c2_url=block.get("c2_url") or lines.get("red.c2_url"),
                     c2_operation=block.get("c2_operation") or lines.get("red.c2_operation"))

    # 1. red01 + the engine DNAT come from the existing (v1) provisioning path,
    #    including its per-competition identity reservation: red01's vmid/IP are
    #    reserved in the comp's OWN config and its deploy stamp lives beside it,
    #    so a validation deploy can never recycle another session's red01.
    red_ip = (red_ip or block.get("red_ip") or getattr(ctx, "red_ip", None))
    if provision is None:
        from red import badauto_config_ops, red_plant_ops, realm_c2_ops
        try:
            engine_vmid = getattr(ctx, "engine_vmid", None) or \
                (getattr(ctx, "state", None) or {}).get("scoring_vm_id")
            base_path, red_vmid, reserved_ip = badauto_config_ops.prepare_deploy_config(
                comp_dir, engine_vmid,
                engine_ip=realm_c2_ops.engine_ip_for(comp_dir), ssh_key=ssh_key)
            red_ip = red_ip or reserved_ip
            provision = red_plant_ops._deploy_red(comp_dir, {**os.environ}, base_path)
        except (TypeError, ValueError) as exc:
            record_degradation("tlaloc_red_config", f"cannot reserve red01 identity: {exc}")
            return {"ok": False, "reason": f"red01 reservation failed: {exc}"}
        except subprocess.TimeoutExpired:
            record_degradation("tlaloc_red_deploy", "red01 provisioning timed out")
            return {"ok": False, "reason": "provision timeout"}
    if getattr(provision, "returncode", 1) != 0:
        record_degradation("tlaloc_red_deploy", _tail(provision))
        print("  tlaloc: red01 provisioning FAILED (see degradations)")
        return {"ok": False, "reason": "provision failed"}
    if not red_ip:
        from red import red_plant_ops
        red_ip = red_plant_ops.red_ip_for(comp_dir)

    # 2. comp-prep: build + freeze the payload matrix while Mythic is idle.
    prep = _default_runner(comp_prep_command(comp_dir), cwd=str(TLALOC_REPO),
                           timeout=1800)
    if getattr(prep, "returncode", 1) != 0:
        record_degradation("tlaloc_c2_prep", _tail(prep))
        print("  tlaloc: c2-prep FAILED — the channel stage will skip "
              "(see degradations)")

    # 3. stage + configure + install + seed.
    staged = stage_red01(ssh_key, red_ip, comp_dir, runner=runner)
    if not staged.get("ok"):
        record_degradation("tlaloc_stage", staged.get("reason", ""))
        return {"ok": False, "reason": "staging failed"}
    config_text = cfg_path.read_text() if cfg_path.exists() else ""
    pushed = push_config(ssh_key, red_ip, config_text, runner=runner)
    if not pushed.get("ok"):
        record_degradation("tlaloc_config_push", pushed.get("detail", ""))
        return {"ok": False, "reason": "config push failed"}
    contract = verify_config_contract(ssh_key, red_ip, intended=cfg_path,
                                      runner=runner)
    if not contract.get("ok"):
        record_degradation("tlaloc_config_contract", json.dumps(contract)[:400])
        print(f"  tlaloc: the pushed config dropped {contract.get('missing')} — "
              f"those features would run on defaults (or be dead) on the VM")
    installed = install_unit(ssh_key, red_ip, runner=runner)
    if not installed.get("ok"):
        record_degradation("tlaloc_unit", installed.get("detail", ""))
        return {"ok": False, "reason": "unit install failed"}
    seeded = seed_via_red01(ssh_key, red_ip, runner=runner)
    summary = {"ok": bool(seeded.get("ok")), "red_ip": red_ip, "engine": "tlaloc",
               "config": str(cfg_path), "at": _now()}
    if not seeded.get("ok"):
        record_degradation("tlaloc_seed", seeded.get("detail", ""))
        print("  tlaloc: seed FAILED (see degradations)")
    try:
        ctx.state["assume_breach"] = summary
        ctx.save_state()
    except Exception:                                  # state bookkeeping is never fatal
        pass
    if seeded.get("ok"):
        print(f"  tlaloc: seeded on red01 {red_ip} — the range is owned before T0 "
              f"(unit {UNIT}, state {STATE_DIR})")
    return summary


def destroy_tlaloc_red(comp_dir, env=None, runner=None, ssh_key=None, red_ip=None):
    """Stop the unit, then run the VM/NAT teardown that owns red01.

    Order matters: stopping `tlaloc.service` ends the event clock and the audit
    log cleanly, and it must happen BEFORE the VM disappears (otherwise the last
    cycle's restore rows are lost and the verdict has no undo table).
    """
    runner = runner or _default_runner
    env = env if env is not None else os.environ
    ssh_key = ssh_key or env.get("TLALOC_SSH_KEY")
    red_ip = red_ip or env.get("TLALOC_RED_IP")
    if not (ssh_key and red_ip):
        # Fall back to what the deploy recorded: the comp's own red config holds
        # red01's address and the deploy's key (never the host's defaults).
        try:
            from red import badauto_config_ops, red_plant_ops
            cfg = badauto_config_ops.load_config(comp_dir)
            ssh_key = ssh_key or ((cfg.get("deploy") or {}).get("ssh_key"))
            red_ip = red_ip or red_plant_ops.red_ip_for(comp_dir)
        except Exception:                              # teardown must still destroy the VM
            pass
    if ssh_key and red_ip:
        print(f"  red01      : stopping tlaloc.service on {red_ip} before teardown...")
        _ssh_run(ssh_key, red_ip, unit_stop_command(), runner=runner, timeout=180)
    teardown = runner(destroy_command(comp_dir),
                      cwd=str(REPO.parent / "bad-auto"),
                      env={**(env if env is not None else os.environ),
                           "BAuto_COMPETITION_DIR": str(Path(comp_dir).resolve())},
                      timeout=900)
    return teardown


# --------------------------------------------------------------------------- #
# verdict + status
# --------------------------------------------------------------------------- #

VERDICT_FILES = ("restore-table.json", "report.md", "report-secrets.md", "world.json")


def collect_evidence(red_state_dir, evidence_dir, *, copy=shutil.copy2):
    """Pull tlaloc's artifacts into the run's evidence tree (before destroy)."""
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    pulled = {}
    for name in VERDICT_FILES:
        source = Path(red_state_dir) / name
        if source.exists():
            target = evidence_dir / name
            copy(source, target)
            pulled[name] = str(target)
    return pulled


def red_verdict(evidence_dir, *, engine="tlaloc"):
    """The machine-readable red verdict the dashboard consumes.

    `coverage` (per-pin down->up proof) is the ENGINE's job — it reads the
    scoreboard, not the agent's opinion. This assembles what only red knows: what
    was planted, what was taken down, and how to undo it, plus the decision log's
    own summary so an after-action can grade adaptivity without parsing prose.
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


def status(comp_dir, *, ssh_key=None, red_ip=None, runner=None):
    """What the engine knows about this competition's red setup."""
    block = red_block(comp_dir)
    out = {"engine": red_engine(comp_dir), "block": block,
           "problems": validate_red_block(block),
           "tlaloc_checkout": str(TLALOC_REPO) if (TLALOC_REPO / "tlaloc").exists()
           else None,
           "unit": None}
    if ssh_key and red_ip:
        proc = _ssh_run(ssh_key, red_ip,
                        f"systemctl is-active {UNIT} 2>/dev/null; "
                        f"systemctl is-enabled {UNIT} 2>/dev/null", runner=runner,
                        timeout=60)
        out["unit"] = (getattr(proc, "stdout", "") or "").split()
    return out


def _now():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")
