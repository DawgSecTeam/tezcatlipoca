"""Native process configuration: the replacement for the TF_VAR_*/.env surface.

Resolution order per key is override -> env -> file -> default. Overrides are the
multi-node activation (`activate`): placing a competition on a satellite must beat a
stale ambient env, and doing it in-process is what lets `nodes_config` stop mutating
os.environ. Env beats file because the dashboard injects per-run values (node tokens,
the engine mgmt IP) into the process environment and those must win over a committed
profile.

`_SPEC` is the only place the key set and its legacy env names live. Legacy names are
accepted until every call site reads `config`; the canonical override for any key is
TEZ_ + the key upper-cased with dots as underscores (the `knobs.` prefix is dropped so
the existing TEZ_* knobs keep their names).
"""

import os
import tomllib
from pathlib import Path

DEFAULT_PATH = "tezcatlipoca.toml"
CONFIG_ENV = "TEZ_CONFIG"
REPO_ROOT = Path(__file__).resolve().parents[1]

# dotted key -> (default, legacy env names). The default's type drives parsing.
_SPEC = {
    "proxmox.endpoint": ("", ("TF_VAR_proxmox_endpoint",)),
    "proxmox.api_token": ("", ("TF_VAR_proxmox_api_token",)),
    "proxmox.node": ("pve", ("TF_VAR_proxmox_node",)),
    "proxmox.datastore": ("local-lvm", ("TF_VAR_datastore",)),
    "proxmox.template_vm_id": (0, ("TF_VAR_template_vm_id",)),
    "proxmox.mgmt_bridge": ("vmbr0", ("TF_VAR_mgmt_bridge",)),
    "proxmox.tls_fingerprint": ("", ("PROXMOX_TLS_FINGERPRINT",)),
    "proxmox.ca_bundle": ("", ("PROXMOX_CA_BUNDLE",)),
    "ssh.public_key": ("", ("TF_VAR_ssh_public_key",)),
    "ssh.private_key_path": ("", ("TF_VAR_ssh_private_key_path",)),
    "engine.username": ("sysadmin", ("TF_VAR_vm_username",)),
    "engine.mgmt_ip": ("", ("TF_VAR_engine_mgmt_ip",)),
    "engine.mgmt_gw": ("", ("TF_VAR_engine_mgmt_gw",)),
    "headscale.url": ("", ()),
    "headscale.ssh_host": ("", ()),
    "headscale.ssh_user": ("", ()),
    "headscale.ssh_password": ("", ()),
    "headscale.infra_user": ("cyberrange-infra", ()),
    "headscale.key_ttl": ("72h", ("TEZ_REMOTE_KEY_TTL",)),
    "portal.hostname": ("", ()),
    "portal.tunnel_token": ("", ()),
    "portal.open": (False, ()),
    "knobs.thin_headroom": (1.0, ()),
    "knobs.cycle_timeout": (600, ()),
    "knobs.python": ("", ()),
    "knobs.red_segment": ("", ()),
    "knobs.red_template": ("", ()),
    "knobs.comps_root": ("", ("TEZ_COMPETITIONS_ROOT",)),
    "knobs.artifacts_archive": ("", ()),
    "knobs.pause_file": ("", ()),
    "knobs.nakon_path": ("", ()),
    "knobs.badauto_path": ("", ()),
    "knobs.opencode_bin": ("", ()),
    "knobs.ops_url": ("", ()),
    "knobs.ops_token": ("", ()),
    "knobs.dashboard_data": ("", ()),
    "knobs.dashboard_host": ("", ()),
    "knobs.dashboard_port": (0, ()),
    "knobs.dashboard_tokens": ("", ()),
    "knobs.engine_env_file": ("", ()),
    "knobs.engine_proxmox_file": ("", ()),
    "knobs.engine_repo_url": ("", ()),
    "knobs.competitions_repo_url": ("", ()),
    "knobs.webui_catalog_file": ("", ()),
    "knobs.scrim_web01_port": (0, ("SCRIM_WEB01_PORT",)),
    "knobs.qga_bridge": ("vmbr0", ()),
    "knobs.qga_pkg_branch": ("", ()),
    "knobs.qga_stage_dir": ("/tmp/pfsense-qga-pkgs", ()),
    "knobs.pfsense_qga_vmid": (0, ()),
    "knobs.pfsense_provision_vmid": (0, ()),
    "knobs.vyos_vmid": (0, ()),
    "knobs.vyos_iso_url": ("", ()),
    "knobs.vyos_iso_storage": ("local", ()),
    "knobs.vyos_iso_dir": ("", ()),
    "knobs.vyos_storage": ("", ()),
    "knobs.vyos_bridge": ("vmbr0", ()),
    "knobs.vyos_pw": ("", ()),
}

# Keys with no usable default: reading one raises rather than yielding "".
_REQUIRED = frozenset((
    "proxmox.endpoint", "proxmox.api_token", "proxmox.template_vm_id",
    "ssh.public_key", "ssh.private_key_path",
))

_TRUE = ("1", "true", "yes", "on")


class ConfigError(SystemExit, RuntimeError):
    """A mandatory config key is missing.

    Dual-derived on purpose: a caller deep in a tolerant path wraps this in
    `except Exception` (the shape the old `os.environ[...]` KeyError had), while at the top
    level it must still exit with a one-line message and no traceback like every other
    operator refusal in this repo.
    """


def env_name(key):
    return "TEZ_" + key.removeprefix("knobs.").upper().replace(".", "_")


def ssh_key_path():
    """The SSH private key, resolved to an absolute path.

    A relative path is the profile's own (repo-root-relative), but the legacy env value is
    terraform-relative (`../proxmox` meaning the checkout root), so both bases are tried and
    an existing file wins. Without a hit the repo-root reading is returned, so the error
    names the path the profile asked for.
    """
    raw = Path(active().ssh.private_key_path)
    if raw.is_absolute():
        return raw
    for base in (REPO_ROOT, REPO_ROOT / "terraform"):
        candidate = (base / raw).resolve()
        if candidate.exists():
            return candidate
    return (REPO_ROOT / raw).resolve()


def _coerce(default, raw):
    if isinstance(default, bool):
        return str(raw).strip().lower() in _TRUE
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return str(raw)


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
        default, legacy = _SPEC[key]
        for name in (env_name(key), *legacy):
            raw = os.environ.get(name)
            if raw not in (None, ""):
                return _coerce(default, raw)
        value = _dig(self._file, key)
        return default if value is None else _coerce(default, value)

    def require(self, key):
        value = self.get(key)
        if value in ("", 0, None):
            raise ConfigError(
                f"  ERROR: config '{key}' is not set. Set it in {self.path} "
                f"(or export {env_name(key)}).")
        return value

    def is_set(self, key):
        """True when the key is present at all — an explicitly empty value counts.

        `get` cannot express the difference between unset and deliberately empty (both
        read as the default), and one caller needs it: an empty engine mgmt IP means
        "DHCP", which must not be re-defaulted to a static address.
        """
        if key in self._overrides:
            return True
        _, legacy = _SPEC[key]
        if any(os.environ.get(name) is not None for name in (env_name(key), *legacy)):
            return True
        return _dig(self._file, key) is not None

    def activate(self, values):
        """Apply in-process overrides; returns a token for `deactivate`."""
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
    """`active().require(key)` — the module-level form call sites use."""
    return active().require(key)


def proxmox_env():
    """The resolved Proxmox identity as TF_VAR_* env, for subprocess-facing callers.

    `activate_placement` moves the whole process onto the engine's host by mutating the config,
    not os.environ, so a caller that snapshotted the environment before activation (or handed a
    dict to a subprocess) still names the profile's host. This is what such a caller re-applies
    after activation — the same five keys the terraform-era activation exported.
    """
    cfg = active()
    return {"TF_VAR_proxmox_endpoint": cfg.proxmox.endpoint,
            "TF_VAR_proxmox_api_token": cfg.proxmox.api_token,
            "TF_VAR_proxmox_node": cfg.proxmox.node,
            "TF_VAR_datastore": cfg.proxmox.datastore,
            "TF_VAR_template_vm_id": str(cfg.proxmox.template_vm_id)}
