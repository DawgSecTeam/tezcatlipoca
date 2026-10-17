"""Test scaffolding: one fake per boundary. No `object.__new__`, no real socket.

`FakeSSH` mirrors `tlaloc.exec.ssh.SSH`'s surface exactly (ssh, ssh_ps, ssh_ok,
run_local, sftp_put, sftp_get, close) and resolves each call against the scripts
registered with `on()`: first match wins, an unmatched call is (0, "", "") —
which is what a happy-path box does. Every call is recorded in `.calls`.
"""

import copy
import json
import shutil
from pathlib import Path

from tlaloc import config as config_mod
from tlaloc.config import apply_compfile, load_config, validate_config
from tlaloc.ctx import Ctx
from tlaloc.effects import reload_registry
from tlaloc.errors import TransportError
from tlaloc.events import EventLog
from tlaloc.exec.gate import Gate
from tlaloc.intel.blind import BlindIntel
from tlaloc.state import World
from tlaloc.state.world import artifact_key  # noqa: F401  (re-exported for tests)

FIXTURES = Path(__file__).parent / "fixtures"
DEFAULT_COMP = FIXTURES / "comp-min"


class FakeSSH:
    """A scripted transport. `on()` rules are matched in registration order."""

    def __init__(self, dry_run=False):
        self.calls = []
        self._rules = []
        self.dry_run = dry_run
        self.closed = False

    # -- scripting ----------------------------------------------------------

    def on(self, match, rc=0, out="", err=""):
        """Register a response. `match` is a substring or a callable(call)->bool."""
        self._rules.append((match, rc, out, err))
        return self

    def sent(self, needle):
        """Every command text sent that contains `needle`."""
        return [c["cmd"] for c in self.calls if needle in (c.get("cmd") or "")]

    def targets(self):
        return [c["ip"] for c in self.calls]

    def _resolve(self, call):
        for match, rc, out, err in self._rules:
            if callable(match):
                if match(call):
                    return rc, out, err
            elif isinstance(match, str) and match in (call.get("cmd") or ""):
                return rc, out, err
        return 0, "", ""

    # -- the SSH surface ----------------------------------------------------

    def ssh(self, ip, user, password, cmd, timeout=None, windows=False,
            key_path=None):
        call = {"ip": ip, "user": user, "password": password, "cmd": cmd,
                "timeout": timeout, "windows": windows, "key_path": key_path}
        self.calls.append(call)
        rc, out, err = self._resolve(call)
        if rc == "raise":
            raise TransportError(f"scripted transport failure for {cmd[:40]}")
        return rc, out, err

    def ssh_ps(self, ip, user, password, script, timeout=None, key_path=None):
        return self.ssh(ip, user, password, script, timeout=timeout,
                        windows=True, key_path=key_path)

    def ssh_ok(self, ip, user, password, windows=False, key_path=None):
        rc, out, _ = self.ssh(ip, user, password, "whoami", windows=windows,
                              key_path=key_path)
        return rc == 0, out.strip()

    def run_local(self, cmd, timeout=None, dry_run=False):
        return self.ssh("local", "red01", None, cmd, timeout=timeout)

    def sftp_put(self, ip, user, password, local_path, remote_path, mode=0o755,
                 key_path=None):
        self.calls.append({"ip": ip, "user": user, "cmd": f"sftp put -> {remote_path}",
                           "kind": "sftp_put", "timeout": None, "windows": False,
                           "key_path": key_path})
        return True

    def sftp_get(self, ip, user, password, remote_path, local_path, key_path=None):
        self.calls.append({"ip": ip, "user": user, "cmd": f"sftp get <- {remote_path}",
                           "kind": "sftp_get", "timeout": None, "windows": False,
                           "key_path": key_path})
        return True

    def close(self):
        self.closed = True


class FakeC2:
    """Mirrors tlaloc.c2.mythic.Mythic: callback evidence + tasking."""

    def __init__(self, callbacks=None, task_out=""):
        self._callbacks = dict(callbacks or {})
        self._task_out = task_out
        self.tasks = []
        self.plants = []

    def callbacks(self):
        """{box_id: {"seen_at": epoch, "agent": str, "external_id": str}}"""
        return dict(self._callbacks)

    def callback_agents(self):
        return {k: v.get("agent") for k, v in self._callbacks.items()}

    def plant(self, box_id, os_name="linux", transport="http",
              callback_uri=None, extra=None):
        self.plants.append({"box": box_id, "os": os_name, "transport": transport,
                            "callback_uri": callback_uri, "extra": extra or {}})
        return {"ok": True, "detail": f"planted {box_id}",
                "payload": f"/var/lib/tlaloc/payloads/{box_id}.bin",
                "uuid": f"uuid-{box_id}"}

    def task(self, box_id, command, timeout=120):
        self.tasks.append({"box": box_id, "command": command})
        return {"ok": True, "output": self._task_out or "[fake c2] ok",
                "task_id": f"task-{len(self.tasks)}"}


class FakeSensors:
    """Mirrors tlaloc.sensors.Sensors: the scoreboard mirror + restore detection."""

    def __init__(self, service_map=None, down_counts=None):
        self.service_map = service_map or {}
        self._down = down_counts or {}
        self.logins = 0
        self.reachable = True

    def poll(self):
        if not self.reachable:
            return None
        return copy.deepcopy(self.service_map)

    def team_service_map(self):
        return self.poll() or {}

    def team_down_counts(self):
        if self._down:
            return dict(self._down)
        counts = {}
        for team, svcs in self.team_service_map().items():
            up = sum(1 for ok in svcs.values() if ok)
            counts[team] = (up, len(svcs) - up)
        return counts


def make_intel(comp_dir=None, blind=False):
    if blind:
        return BlindIntel(str(comp_dir or DEFAULT_COMP))
    from tlaloc.intel.nakon import NakonIntel
    return NakonIntel(str(comp_dir or DEFAULT_COMP))


def base_cfg(**over):
    """A validated config with no file layer (never reads the repo's config.yaml)."""
    cfg = copy.deepcopy(config_mod.DEFAULTS)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key] = config_mod._deep_merge(cfg[key], value)
        else:
            cfg[key] = value
    validate_config(cfg)
    if not cfg["run"]["kill_file"]:
        cfg["run"]["kill_file"] = str(Path(cfg["run"]["state_dir"]) / "KILL")
    return cfg


def make_ctx(tmp_path, *, cfg=None, comp_dir=None, blind=False, dry_run=False,
             c2=None, sensors=None, ssh=None, policy=None, comp="tlaloc-smoke",
             compfile=True):
    """A live Ctx on a temp state dir, wired to fakes."""
    reload_registry()
    state_dir = Path(tmp_path) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg or base_cfg()
    cfg["run"]["state_dir"] = str(state_dir)
    cfg["run"]["comp"] = comp
    cfg["run"]["kill_file"] = str(state_dir / "KILL")
    if comp_dir:
        # production parity: build_context stamps the resolved comp dir into the
        # config, and the payload manifest is looked up relative to it
        cfg["run"]["comp_dir"] = str(Path(comp_dir).resolve())
    if comp_dir and compfile:
        # compfile=False keeps the fixture's intel while leaving the caller's
        # own red.* block alone (the fixture Compfile pins assume_breach).
        cfg = apply_compfile(cfg, comp_dir)
    world = World(state_dir, comp=comp)
    intel = make_intel(comp_dir, blind=blind)
    gate = Gate(cfg, intel.subnets())
    ctx = Ctx(cfg=cfg, world=world, log=EventLog(state_dir), intel=intel, gate=gate,
              ssh=ssh or FakeSSH(), c2=c2, sensors=sensors, dry_run=dry_run,
              comp=comp)
    ctx.policy = policy
    return ctx


def read_events(ctx):
    return ctx.log.read()


def write_fixture(dst, files):
    """Copy a fixture comp elsewhere and override files (for mutating tests)."""
    dst = Path(dst)
    shutil.copytree(files, dst, dirs_exist_ok=True)
    return dst


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
