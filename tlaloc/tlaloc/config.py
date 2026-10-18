"""Config: DEFAULTS < config.yaml < env allow-map. PURE — nothing aliases DEFAULTS.

The earlier agent's defect (verified empirically in the audit): ``cfg = DEFAULTS``
plus a shallow merge left every non-overridden sub-dict aliased into the
module-level DEFAULTS, so one load_config() call leaked its env overrides into
every later call in the process. tlaloc deep-copies and documents: config is
frozen once the context is built — anything that needs to change mid-run lives in
the Compfile or world state, not in cfg mutation.

An explicit --config path that does not exist is a HARD error (the earlier agent
silently fell back to DEFAULTS — the wrong-red_vmid incident). API keys never
enter cfg: only the NAME of the env var to read them from.

ROE lives in data: the ``red:`` block (ladder, caps, windows) mirrors the
Compfile's ``red.*`` keys and is validated at load — the earlier agent's
420-minute clock hardcoded in escalation.py misfired on shorter events; here
every time is either an explicit minute value or a fraction of ``red.duration_min``.
"""

import copy
import os
import re
from pathlib import Path

import yaml

from .errors import ConfigError

ENV_PREFIX = "TLALOC_"

DEFAULTS = {
    "run": {
        "comp": "",                # competition id, stamped into world.json
        "state_dir": ".state",     # fresh dir per comp (cross-event leak fix)
        "comp_dir": "",            # the comp's directory (set from --comp/deploy)
        "kill_file": "",           # empty -> <state_dir>/KILL
        "dry_run": False,
        "decision_window_min": 4,  # baseline cycle cadence
        "focus_rotation_min": 12,  # how often the pressure focus rotates teams
        "jitter_pct": 15,
        "endgame_cycle_min": 1.0,  # cadence while the final window is open
        "error_halt_after": 8,     # consecutive cycle errors before the loop stops
        "host_guard": True,        # refuse the live loop off red01 (see run.py)
        "blue_ips": [],            # extra IPs in scope (e.g. a human-blue laptop)
    },
    "red": {                       # the ROE block — mirror of Compfile `red.*`
        "assume_breach": "full",   # full | light | off   (seed pipeline reads it)
        "depth": 3,                # seed depth 0..3 for `full`
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
        # The endpoint's batch ceiling, in tokens. clef runs with a 16k
        # context (was 512 at --ubatch-size 512), so the
        # digest no longer has to be squeezed to fit: the whole state goes out.
        # The truncation cascade in state/digest.py stays as a guard for a
        # smaller endpoint or another model — it is sized against this number.
        "max_request_tokens": 16384,
        # Conservative estimate; ClefClient learns the endpoint's real ratio from
        # its own "input (N tokens) is too large" message (see state/digest.py).
        "tokens_per_char": 0.7,
        "theta": 0.60,               # default confidence gate (top probability)
        "theta_by_question": {},     # per-question overrides from scrim logs
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
        "enabled": False,          # Mythic (installed/configured by Gaia)
        "base_url": "",
        "username": "tlaloc",
        "password_env": "TLALOC_MYTHIC_PASSWORD",
        "operation": "",           # one operation per comp, created at comp-prep
        "verify_tls": False,
        "callback_max_age_sec": 120,
        # The callback address a built payload dials, as the team boxes see it —
        # the engine's DNAT gateway for the C2 port. Without it the payload
        # inherits the Mythic profile instance's own default (Mythic installs
        # with a loopback callback_host), and the channel can never check in.
        # "" / 0 = leave the profile instance's value alone.
        "callback_host": "",
        "callback_port": 0,
        # implant_channel waits this long for a fresh callback after a plant;
        # task_channel bounds one task's polling with task_timeout_sec.
        "plant_wait_sec": 45,
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
        "command_max_len": 200,    # free-form commands (task/improvise)
        "cred_spray_max": 40,
        # spray_access: per-login probe deadline and the human-mimic gap between
        # logins (the earlier agent's pacing; 0 in tests).
        "spray_probe_timeout": 20,
        "spray_delay_sec": 0.4,
        "nmap_top_ports": 200,
        "nmap_timing": "T4",
        "scan_timeout": 180,
    },
    "naming": {
        # full <stem>-<suffix> | stem | suffix — see effects/artifactnames.py.
        # The half blue matches on is the half to drop; `full` is the default
        # because it is the most realistic, not because it is the safest.
        "mode": "full",
    },
    "effects": {
        "allow_ssh_takedown": False,   # scoring sshd down needs explicit opt-in
        "rekill_cron": True,           # persistence family: re-stop restored units
        "rotate_credlist": True,
        "decoy": False,                # procfilter hunt-bait (optional flavour)
        "mysql_udf": False,            # MySQL UDF OS-exec (optional flavour)
    },
    # Optional flavour ASSETS. Empty by default: the effects draw their own
    # locations from effects/artifactnames.py, and these only point at a vendored
    # asset (the earlier agent's preload .so / procfilter tree). They are read
    # defensively — which
    # is exactly why they must be declared: an undeclared key is a feature that is
    # silently dead on red01, because the pushed config drops it.
    "evasion": {
        "preload_so": "",              # path to the hider .so on red01, if vendored
    },
    "procfilter": {
        "install_dir": "",             # path to the procfilter tree, if vendored
    },
}

# env var -> dotted config path. Deliberately short: anything bigger belongs in
# the config file (the earlier agent's sprawl lesson: three tracked configs, all
# divergent).
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
    "TLALOC_EVENT_MIN": "red.duration_min",
    "TLALOC_QUOTIENT_URL": "sensors.quotient_url",
}

_TRUE_FALSE = {"1": True, "true": True, "yes": True,
               "0": False, "false": False, "no": False}


def _coerce(text):
    """'5'->5, '1.5'->1.5, 'true'->True, else the string."""
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
    """Read repo/.env then /etc/tlaloc/env into os.environ (never overrides)."""
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
    """Load the three layers, PURELY. Raises ConfigError on a missing explicit
    path or an invalid ROE block; never mutates DEFAULTS."""
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


# -- Compfile integration -----------------------------------------------------
# The tezcatlipoca Compfile is `key value` lines (see the engine's
# common/utils.py compfile_flag/compfile_value), NOT yaml. The contract:
#   red.assume_breach full            dotted scalar keys
#   red.caps.max_down 4
#   red_yaml red.yaml                 pointer to a nested block (ladder etc.),
#                                     path relative to the comp dir
def load_compfile_red(comp_dir):
    """Extract the red block from a comp dir's Compfile (may be {})."""
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
    """Merge the Compfile red block over cfg.red and revalidate. The Compfile is
    the authority for ROE values (deploy-time contract); config.yaml provides
    the agent's own settings."""
    red = load_compfile_red(comp_dir)
    if red:
        cfg["red"] = _deep_merge(cfg["red"], red)
    normalize(cfg)
    validate_config(cfg)
    return cfg


def normalize(cfg):
    """Fold YAML's booleans back into the enums they were written as.

    `assume_breach: off` is three characters an operator WILL write, and YAML
    1.1 parses it as `False` — which then fails validation with a message about
    a value nobody typed. Normalize once, at load, in one place.
    """
    red = cfg.get("red") or {}
    value = red.get("assume_breach")
    if isinstance(value, bool):
        red["assume_breach"] = "full" if value else "off"
    return cfg


# -- validation ---------------------------------------------------------------

def validate_config(cfg):
    """Fail loud at load (the deploy-time validation the plan demands)."""
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
                # earlier defect: at_min was divided by a hardcoded 420-min packet
                # clock, so a 60-min scrim silently rescaled every tier.
                # Conversion happens here, against THIS event's duration, and is
                # written back into the list — a conversion nobody can observe
                # would leave every tier at pct 0 and fire the whole ladder at
                # T+0.
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
    return cfg
