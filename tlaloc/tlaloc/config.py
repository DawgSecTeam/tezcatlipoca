"""Config: DEFAULTS < config.yaml < env allow-map. PURE — nothing aliases DEFAULTS."""

import copy
import os
import re
from pathlib import Path

import yaml

from .errors import ConfigError

ENV_PREFIX = "TLALOC_"

DEFAULTS = {
    "run": {
        "comp": "",
        "state_dir": ".state",     # fresh dir per comp — sharing one across events leaks state
        "comp_dir": "",
        "kill_file": "",           # empty -> <state_dir>/KILL
        "dry_run": False,
        "decision_window_min": 4,
        "focus_rotation_min": 12,
        "jitter_pct": 15,
        "endgame_cycle_min": 1.0,
        "error_halt_after": 8,
        "host_guard": True,        # refuse the live loop off red01
        "blue_ips": [],            # extra IPs in scope (e.g. a human-blue laptop)
    },
    "red": {                       # the ROE block — mirror of Compfile `red.*`
        "assume_breach": "full",   # full | light | off   (seed pipeline reads it)
        "depth": 3,
        "duration_min": 150,       # the event clock — the ONLY clock source
        "ladder": {
            "enabled": True,
            "tiers": [
                {"name": "observe", "at_pct": 0.00, "modes": ["recon"],
                 "boxes_per_team": 0},
                {"name": "easy",    "at_pct": 0.15, "modes": ["stop"],
                 "boxes_per_team": 1},
                {"name": "medium",  "at_pct": 0.55, "modes": ["stop", "stop_mask"],
                 "boxes_per_team": 2},
                {"name": "hard",    "at_pct": 0.72,
                 "modes": ["stop", "stop_mask", "block_port"], "boxes_per_team": 2},
                {"name": "brutal",  "at_pct": 0.86,
                 "modes": ["stop", "stop_mask", "block_port", "stop_rename_config"],
                 "boxes_per_team": 4, "unrecoverable": True},
            ],
            # mercy rule: recent blue remediation shifts the ladder clock back
            "pause_after_restore_min": 10,
        },
        # max_down 0 means NO LIMIT on concurrent takedowns per team (owner
        # directive 2026-10-18: red must be free to split blue's attention).
        "caps": {"max_down": 0, "min_standing": 1, "grace_min": 10},
        # The observe tier is a recon pass, not a ten-minute no-op: after this
        # long the first stop is legal even while the ladder still says observe.
        "opening_takedown_min": 2,
        "windows": {"endgame_teardown": 15, "lockout": 3},  # minutes before end
        "budget": {"extra_pressure_max": 2, "improvise_rounds": 2,
                   "improvise_commands": 6},
    },
    "clef": {
        "enabled": True,
        "base_url": "http://203.0.113.30:8080",
        "path": "/v1/systemone",
        "model": "clef-flash",
        "timeout_sec": 5,
        # Batch ceiling in tokens; the truncation cascade in state/digest.py is sized
        # against it as a guard for a smaller endpoint.
        "max_request_tokens": 16384,
        # conservative; ClefClient learns the real ratio from the endpoint's own
        # "input (N tokens) is too large" message
        "tokens_per_char": 0.7,
        "theta": 0.60,               # default confidence gate (top probability)
        "theta_by_question": {},
        "high_stakes_effects": ["takedown", "lockout", "rotate_credlist"],
    },
    "llm": {                       # the arbiter/generator — never the decider
        "enabled": False,
        "base_url": "",
        "model": "",
        "timeout_sec": 60,
        "api_key_env": "TLALOC_LLM_API_KEY",
    },
    "c2": {
        "engine": "sliver",        # sliver (default) | mythic (legacy Gaia path)
        "enabled": False,          # installed/configured by the deploy's C2 step
        "base_url": "",
        # Sliver operator profile (mTLS certs + token); the deploy writes it 0600
        # on red01 and the config carries the path, never the secret.
        "operator_config": "/etc/tlaloc/sliver.cfg",
        "dns_domain": "c2.sliver.range",
        # Per-transport gateway/server port overrides, e.g.
        # {mtls: {gateway: 8888, server: 8888}}. Empty uses the deploy defaults.
        "transports": {},
        "username": "tlaloc",
        "password_env": "TLALOC_MYTHIC_PASSWORD",
        "operation": "",
        "verify_tls": False,
        "callback_wait_sec": 240,
        "callback_max_age_sec": 120,
        # "draw" (default): one implant per box, chosen per box. "full": EVERY
        # transport on every box — for coverage/QA runs that prove each path.
        "implant_spread": "draw",
        # Mythic only: the callback address a built payload dials.
        "callback_host": "",
        "callback_port": 0,
        # implant_channel waits this long for a fresh callback after a plant;
        # task_channel bounds one task's polling with task_timeout_sec.
        "task_timeout_sec": 120,
    },
    "sensors": {
        "quotient_enabled": True,
        "quotient_url": "",        # empty -> intel.discover_engine_ip()
        "poll_timeout_sec": 10,
        "tempo_press_window_min": 1.5,
        "tempo_ease_window_min": 4,
        "ease_silence_min": 30,
        "blue_hunting_every_n_cycles": 6,
    },
    "limits": {
        "action_timeout": 90,
        "max_retries_per_service": 6,
        "unreachable_halt_after": 8,
        "command_max_len": 200,
        "cred_spray_max": 40,
        # spray_access: per-login probe deadline and human-mimic gap (0 delay in tests)
        "spray_probe_timeout": 20,
        "spray_delay_sec": 0.4,
        "nmap_top_ports": 200,
        "nmap_timing": "T4",
        "scan_timeout": 180,
    },
    "naming": {
        # full | stem | suffix — see effects/artifactnames.py; `full` default = most realistic
        "mode": "full",
    },
    "effects": {
        "allow_ssh_takedown": False,   # scoring sshd down needs explicit opt-in
        "rekill_cron": True,           # persistence family: re-stop restored units
        "rotate_credlist": True,
        "decoy": False,
        "mysql_udf": False,
    },
    # Vendored-asset pointers only — effects draw their own locations from
    # effects/artifactnames.py. An undeclared key is dropped by the pushed config: a
    # feature silently dead on red01.
    "evasion": {
        "preload_so": "",              # path to the hider .so on red01, if vendored
    },
    "procfilter": {
        "install_dir": "",             # path to the procfilter tree, if vendored
    },
}

# env var -> dotted config path; anything bigger belongs in the config file
ENV_MAP = {
    "TLALOC_STATE_DIR": "run.state_dir",
    "TLALOC_COMP": "run.comp",
    "TLALOC_KILL_FILE": "run.kill_file",
    "TLALOC_DRY_RUN": "run.dry_run",
    "TLALOC_CLEF_URL": "clef.base_url",
    "TLALOC_CLEF_DISABLED": "clef.enabled",
    "TLALOC_LLM_URL": "llm.base_url",
    "TLALOC_LLM_MODEL": "llm.model",
    "TLALOC_MYTHIC_URL": "c2.base_url",
    "TLALOC_SLIVER_URL": "c2.base_url",
    "TLALOC_SLIVER_CONFIG": "c2.operator_config",
    "TLALOC_C2_ENGINE": "c2.engine",
    "TLALOC_EVENT_MIN": "red.duration_min",
    "TLALOC_QUOTIENT_URL": "sensors.quotient_url",
}

_TRUE_FALSE = {"1": True, "true": True, "yes": True,
               "0": False, "false": False, "no": False}


def _coerce(text):
    low = text.strip().lower()
    if low in _TRUE_FALSE:
        return _TRUE_FALSE[low]
    if re.fullmatch(r"-?\d+", text.strip()):
        return int(text.strip())
    try:
        return float(text.strip())
    except ValueError:
        return text.strip()


def _deep_merge(base, over):
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _set_dotted(cfg, dotted, value):
    node = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise ConfigError(f"env override {dotted} collides with {part}")
    node[parts[-1]] = value


def load_dotenv():
    for path in (Path(__file__).resolve().parent.parent / ".env",
                 Path("/etc/tlaloc/env")):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_config(path=None):
    load_dotenv()
    cfg = copy.deepcopy(DEFAULTS)

    if path:
        if not Path(path).exists():
            raise ConfigError(f"config file {path} does not exist (refusing to "
                              "silently run on defaults — earlier incident)")
    else:
        path = os.environ.get(ENV_PREFIX + "CONFIG") or None
        if path is None:
            repo_cfg = Path(__file__).resolve().parent.parent / "config.yaml"
            path = str(repo_cfg) if repo_cfg.exists() else None
    if path:
        with open(path, encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"config file {path} is not a mapping")
        cfg = _deep_merge(cfg, loaded)

    for env_name, dotted in ENV_MAP.items():
        if env_name in os.environ:
            _set_dotted(cfg, dotted, _coerce(os.environ[env_name]))

    if not cfg["run"]["kill_file"]:
        cfg["run"]["kill_file"] = str(Path(cfg["run"]["state_dir"]) / "KILL")

    normalize(cfg)
    validate_config(cfg)
    return cfg


def load_compfile_red(comp_dir):
    comp_dir = Path(comp_dir)
    path = comp_dir / "Compfile"
    red = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return red
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or " " not in line:
            continue
        key, value = line.split(" ", 1)
        if key == "red_yaml":
            block = comp_dir / value.strip()
            data = yaml.safe_load(block.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise ConfigError(f"{block} must contain a mapping")
            red = _deep_merge(red, data)
        elif key.startswith("red."):
            _set_dotted(red, key[len("red."):], _coerce(value))
    return red


def apply_compfile(cfg, comp_dir):
    """The Compfile is the authority for ROE values; config.yaml is the agent's own settings."""
    red = load_compfile_red(comp_dir)
    if red:
        cfg["red"] = _deep_merge(cfg["red"], red)
    normalize(cfg)
    validate_config(cfg)
    return cfg


def normalize(cfg):
    """YAML 1.1 parses `assume_breach: off` as False; fold booleans back into the enums."""
    red = cfg.get("red") or {}
    value = red.get("assume_breach")
    if isinstance(value, bool):
        red["assume_breach"] = "full" if value else "off"
    return cfg


def validate_config(cfg):
    red = cfg.get("red") or {}
    if red.get("assume_breach") not in ("full", "light", "off"):
        raise ConfigError(f"red.assume_breach must be full|light|off, "
                          f"got {red.get('assume_breach')!r}")
    duration = red.get("duration_min")
    if not isinstance(duration, (int, float)) or duration <= 0:
        raise ConfigError("red.duration_min must be a positive number of minutes")
    if not 0 <= int(red.get("depth", 3)) <= 3:
        raise ConfigError("red.depth must be 0..3")

    ladder = red.get("ladder") or {}
    tiers = ladder.get("tiers") or []
    if ladder.get("enabled", True):
        if not tiers:
            raise ConfigError("red.ladder.tiers must not be empty when enabled")
        for i, tier in enumerate(tiers):
            name = tier.get("name") or f"tier{i}"
            if "at_min" in tier:
                # Conversion against THIS event's duration is written back into the
                # list: an unobserved conversion would leave every tier at pct 0 and
                # fire the whole ladder at T+0.
                if "at_pct" in tier:
                    raise ConfigError(f"ladder tier {name}: use at_pct OR at_min")
                converted = dict(tier)
                converted["at_pct"] = converted.pop("at_min") / float(duration)
                tiers[i] = converted
                tier = converted
            pct = tier.get("at_pct")
            if not isinstance(pct, (int, float)) or not 0.0 <= pct <= 1.0:
                raise ConfigError(f"ladder tier {name}: at_pct must be 0..1")
            if not isinstance(tier.get("boxes_per_team", 0), int):
                raise ConfigError(f"ladder tier {name}: boxes_per_team must be int")
        if tiers and not tiers == sorted(
                tiers, key=lambda t: t.get("at_pct", t.get("at_min", 0) / duration)):
            raise ConfigError("ladder tiers must be sorted by at_pct")

    caps = red.get("caps") or {}
    if caps.get("min_standing", 1) < 1:
        raise ConfigError("red.caps.min_standing must be >= 1 "
                          "(a team must always have one service the pin can reach)")
    if caps.get("max_down", 1) < 0:
        raise ConfigError("red.caps.max_down must be >= 0")

    clef = cfg.get("clef") or {}
    if not 0.0 < float(clef.get("theta", 0.6)) <= 1.0:
        raise ConfigError("clef.theta must be in (0, 1]")
    if clef.get("max_request_tokens", 512) < 128:
        raise ConfigError("clef.max_request_tokens below 128 cannot carry a bundle")

    engine = str((cfg.get("c2") or {}).get("engine") or "sliver").lower()
    if engine not in ("sliver", "mythic"):
        raise ConfigError(f"c2.engine must be sliver|mythic, got {engine!r}")
    return cfg
