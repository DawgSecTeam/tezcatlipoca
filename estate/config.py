"""Native process configuration: the tezcatlipoca.toml surface and its env overrides."""

import os
import tomllib
from pathlib import Path

DEFAULT_PATH = "tezcatlipoca.toml"
CONFIG_ENV = "TEZ_CONFIG"
REPO_ROOT = Path(__file__).resolve().parents[1]

# dotted key -> default. The default's type drives parsing.
_SPEC = {
    "proxmox.endpoint": "",
    "proxmox.api_token": "",
    "proxmox.node": "pve",
    "proxmox.datastore": "local-lvm",
    "proxmox.template_vm_id": 0,
    "proxmox.mgmt_bridge": "vmbr0",
    "proxmox.tls_fingerprint": "",
    "proxmox.ca_bundle": "",
    "ssh.public_key": "",
    "ssh.private_key_path": "",
    "engine.username": "sysadmin",
    "engine.mgmt_ip": "",
    "engine.mgmt_gw": "",
    "headscale.url": "",
    "headscale.ssh_host": "",
    "headscale.ssh_user": "",
    "headscale.ssh_password": "",
    "headscale.infra_user": "cyberrange-infra",
    "headscale.key_ttl": "72h",
    "portal.hostname": "",
    "portal.tunnel_token": "",
    "portal.open": False,
    "knobs.thin_headroom": 1.0,
    "knobs.cycle_timeout": 600,
    "knobs.python": "",
    "knobs.competitions_root": "",
    "knobs.gaia_path": "",
    "knobs.team_identifiers": "",
    "knobs.scoring_vmid": 0,
    "knobs.artifacts_archive": "",
    "knobs.pause_file": "",
    "knobs.scrim_web01_port": 0,
    "knobs.qga_bridge": "vmbr0",
    "knobs.qga_pkg_branch": "",
    "knobs.qga_stage_dir": "/tmp/pfsense-qga-pkgs",
    "knobs.pfsense_qga_vmid": 0,
    "knobs.pfsense_provision_vmid": 0,
    "knobs.vyos_vmid": 0,
    "knobs.vyos_iso_url": "",
    "knobs.vyos_iso_storage": "local",
    "knobs.vyos_storage": "",
    "knobs.vyos_bridge": "vmbr0",
    "knobs.vyos_pw": "",
}

_TRUE = ("1", "true", "yes", "on")


class ConfigError(SystemExit, RuntimeError):
    """A mandatory config key is missing: dual-derived so a tolerant path can catch it while a top-level caller still exits with a one-line message."""


def env_name(key):
    return "TEZ_" + key.removeprefix("knobs.").upper().replace(".", "_")


def ssh_key_path():
    """The SSH private key, resolved to an absolute path against the checkout root."""
    raw = Path(active().ssh.private_key_path)
    if raw.is_absolute():
        return raw
    return (REPO_ROOT / raw).resolve()


def _coerce(key, default, raw):
    if isinstance(default, bool):
        return str(raw).strip().lower() in _TRUE
    if isinstance(default, int):
        return _number(int, key, raw)
    if isinstance(default, float):
        return _number(float, key, raw)
    return str(raw)


def _number(cast, key, raw):
# A non-numeric value refuses by name rather than silently taking the default.
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"  ERROR: config '{key}' must be a number, got {raw!r} — fix it "
                          f"in the profile or {env_name(key)}.")


def _dig(document, key):
    node = document
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _read_toml(path):
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"  ERROR: {path} is not valid TOML: {e}")


class _Section:
    __slots__ = ("_config", "_prefix")

    def __init__(self, config, prefix):
        self._config = config
        self._prefix = prefix

    def __getattr__(self, name):
        key = f"{self._prefix}.{name}"
        if key not in _SPEC:
            raise AttributeError(f"no config key '{key}'")
        return self._config.get(key)


class Config:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get(CONFIG_ENV) or DEFAULT_PATH)
        self._file = _read_toml(self.path)
        self._overrides = {}
        for prefix in ("proxmox", "ssh", "engine", "headscale", "portal", "knobs"):
            setattr(self, prefix, _Section(self, prefix))

    def get(self, key):
        if key in self._overrides:
            return self._overrides[key]
        default = _SPEC[key]
        raw = os.environ.get(env_name(key))
        if raw not in (None, ""):
            return _coerce(key, default, raw)
        value = _dig(self._file, key)
        return default if value is None else _coerce(key, default, value)

    def require(self, key):
        value = self.get(key)
        if value in ("", 0, None):
            raise ConfigError(
                f"  ERROR: config '{key}' is not set. Set it in {self.path} "
                f"(or export {env_name(key)}).")
        return value

    def is_set(self, key):
        """True when the key is present at all — an explicitly empty value counts (an empty engine mgmt IP means DHCP)."""
        if key in self._overrides:
            return True
        if os.environ.get(env_name(key)) is not None:
            return True
        return _dig(self._file, key) is not None

    def activate(self, values):
        previous = {k: self._overrides.get(k) for k in values}
        self._overrides.update(values)
        return previous

    def deactivate(self, previous):
        for key, value in previous.items():
            if value is None:
                self._overrides.pop(key, None)
            else:
                self._overrides[key] = value


_ACTIVE = None


def active():
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = Config()
    return _ACTIVE


def configure(path=None):
    global _ACTIVE
    _ACTIVE = Config(path)
    return _ACTIVE


def reset():
    global _ACTIVE
    _ACTIVE = None


def require(key):
    return active().require(key)


def require_proxmox_identity():
    """Refuse when the ambient config carries neither a Proxmox endpoint nor an API token: teardown's verify_clean reads the ambient config even when nodes.json supplied the deploy's endpoint."""
    cfg = active()
    if cfg.get("proxmox.endpoint") or cfg.get("proxmox.api_token"):
        return
    raise ConfigError(
        f"  ERROR: config 'proxmox.endpoint' (or 'proxmox.api_token') is not set. Set the "
        f"estate identity in {cfg.path} (or export TEZ_PROXMOX_ENDPOINT / "
        f"TEZ_PROXMOX_API_TOKEN) — teardown's verify_clean needs it even when nodes.json "
        f"supplied the deploy's endpoint.")


def proxmox_env():
    """The resolved Proxmox identity as TEZ_* env, for callers that snapshotted the environment before activation."""
    cfg = active()
    return {
        env_name("proxmox.endpoint"): cfg.proxmox.endpoint,
        env_name("proxmox.api_token"): cfg.proxmox.api_token,
        env_name("proxmox.node"): cfg.proxmox.node,
        env_name("proxmox.datastore"): cfg.proxmox.datastore,
        env_name("proxmox.template_vm_id"): str(cfg.proxmox.template_vm_id),
    }

