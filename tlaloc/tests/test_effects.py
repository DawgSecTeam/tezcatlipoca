"""The execute() template, the registry contract, and the reference effects.

The template is where the earlier agent's bypasses died: kill switch,
ensure_target, the lint, the precondition, the dry-run short-circuit and the
ledger/artifact/restore bookkeeping are all outside `run()`, so no effect can
forget them.
"""

import pytest

from tests.helpers import FakeSSH, base_cfg, make_ctx
from tlaloc import effects
from tlaloc.effects import ALL_TAGS, REGISTRY, execute, reload_registry
from tlaloc.errors import EffectParamError, GuardrailError

NMAP_XML = """<?xml version="1.0"?>
<nmaprun><host><ports>
 <port protocol="tcp" portid="22"><state state="open"/><service name="ssh" product="OpenSSH" version="9.6"/></port>
 <port protocol="tcp" portid="80"><state state="open"/><service name="http" product="nginx" version="1.24"/></port>
 <port protocol="tcp" portid="3306"><state state="open"/><service name="mysql"/></port>
</ports></host></nmaprun>"""


# --- registry -----------------------------------------------------------------

def test_registry_is_complete_and_well_formed():
    reload_registry()
    assert effects.MISSING_MODULES == [], (
        "every effect module in effects.MODULES must exist: "
        f"missing {effects.MISSING_MODULES}")
    assert set(REGISTRY) >= {"recon", "takedown", "spray_access", "reestablish",
                             "escalate", "block_port", "rotate_credlist", "lockout",
                             "persist", "implant_channel", "task_channel", "improvise"}
    for effect_id, effect in REGISTRY.items():
        assert effect.id == effect_id
        assert effect.name and effect.description
        assert set(effect.tags) <= ALL_TAGS, effect_id
        assert effect.params.get("ip", {}).get("required") is True, \
            f"{effect_id} must declare a required ip param"
        assert callable(effect.run)


def test_catalog_is_machine_readable():
    reload_registry()
    catalog = effects.catalog()
    assert [e["id"] for e in catalog] == sorted(REGISTRY)
    assert all({"id", "name", "description", "tags", "params"} <= set(e)
               for e in catalog)


def test_unknown_effect_is_a_param_error(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(EffectParamError):
        execute("no_such_effect", {}, ctx)


# --- the template -------------------------------------------------------------

def test_kill_switch_blocks_every_command_path(tmp_path):
    ctx = make_ctx(tmp_path)
    open(ctx.cfg["run"]["kill_file"], "w").write("stop")
    with pytest.raises(GuardrailError):
        execute("recon", {"ip": "192.168.1.10"}, ctx)


def test_off_target_ip_is_a_guardrail_breach(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(GuardrailError):
        execute("recon", {"ip": "203.0.113.99"}, ctx)


def test_dry_run_short_circuits_before_the_transport(tmp_path):
    ssh = FakeSSH()
    ctx = make_ctx(tmp_path, dry_run=True, ssh=ssh)
    result = execute("recon", {"ip": "192.168.1.10"}, ctx)
    assert result.ok and "dry-run" in result.detail
    assert ssh.calls == []
    assert ctx.world.impacts() == []


def test_policy_rejection_is_a_failed_result_not_a_crash(tmp_path):
    ctx = make_ctx(tmp_path)
    from tlaloc.policy import Policy
    ctx.policy = Policy(ctx.cfg, ctx.world, ctx.intel)
    result = execute("takedown", {"ip": "192.168.1.10", "service": "nginx",
                                  "mode": "stop_mask"}, ctx)
    assert result.ok is False
    assert "precondition" in result.detail
    assert ctx.world.reject_memory()          # the rejection is remembered


def test_a_raising_effect_becomes_ok_false(tmp_path):
    ssh = FakeSSH().on("whoami", rc="raise")
    ctx = make_ctx(tmp_path, ssh=ssh)

    class Boom(effects.Effect):
        id = "boom_test"
        params = {"ip": {"type": "str", "required": True}}

        def run(self, ctx, params):
            raise RuntimeError("box exploded")

    REGISTRY["boom_test"] = Boom()
    try:
        result = execute("boom_test", {"ip": "192.168.1.10"}, ctx)
        assert result.ok is False and "box exploded" in result.detail
    finally:
        REGISTRY.pop("boom_test", None)


def test_the_template_writes_the_ledger_artifact_and_restore(tmp_path):
    ctx = make_ctx(tmp_path)

    class Fake(effects.Effect):
        id = "ledger_test"
        params = {"ip": {"type": "str", "required": True}}

        def run(self, ctx, params):
            return effects.Result(True, "did it", impact={"ip": params["ip"],
                                                          "service": "nginx",
                                                          "mode": "stop"},
                                  artifacts=[{"ip": params["ip"], "kind": "persist-cron",
                                              "detail": "cron (/etc/cron.d/x)",
                                              "check_cmd": "test -f /etc/cron.d/x",
                                              "paths": ["/etc/cron.d/x"]}],
                                  restore={"recipe": ["systemctl start nginx"],
                                           "secrets": None})

    REGISTRY["ledger_test"] = Fake()
    try:
        result = execute("ledger_test", {"ip": "192.168.1.10"}, ctx, cycle=3)
        assert result.ok
        assert ctx.world.impacts()[0]["service"] == "nginx"
        assert ctx.world.artifacts()[0]["kind"] == "persist-cron"
        assert ctx.world.artifacts()[0]["paths"] == ["/etc/cron.d/x"]
        assert ctx.world.restore_table()[0]["recipe"] == ["systemctl start nginx"]
        assert any(e["kind"] == "action" for e in ctx.log.read())
    finally:
        REGISTRY.pop("ledger_test", None)


def test_freeform_params_are_linted_by_the_template(tmp_path):
    ctx = make_ctx(tmp_path)

    class Free(effects.Effect):
        id = "free_test"
        params = {"ip": {"type": "str", "required": True},
                  "command": {"type": "str", "required": True, "freeform": True}}

        def run(self, ctx, params):
            return effects.Result(True, "ran")

    REGISTRY["free_test"] = Free()
    try:
        with pytest.raises(GuardrailError):
            execute("free_test", {"ip": "192.168.1.10",
                                  "command": "rm -rf /var/www"}, ctx)
        assert execute("free_test", {"ip": "192.168.1.10",
                                     "command": "systemctl restart nginx"}, ctx).ok
    finally:
        REGISTRY.pop("free_test", None)


def test_param_validation_rejects_shape_errors(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(EffectParamError):
        execute("recon", {}, ctx)                       # missing required ip
    with pytest.raises(EffectParamError):
        execute("recon", {"ip": "192.168.1.10", "nope": 1}, ctx)


def test_every_registered_effect_survives_dry_run(tmp_path, monkeypatch):
    """The phase-1 exit gate: dry-run covers every effect in the catalogue.

    Dry-run short-circuits `run()` but NOT `check()`, deliberately: a dry run
    that skipped preconditions would not tell an operator which candidates the
    ROE rejects. So the fixture here satisfies every precondition honestly — a
    root foothold on the box, the final window open (every mechanism legal), a
    live C2 callback and a frozen manifest — and asserts nothing was touched.
    """
    reload_registry()
    monkeypatch.setattr("tlaloc.effects.recon.shutil.which",
                        lambda name: f"/usr/bin/{name}")
    samples = {
        "recon": {"ip": "192.168.1.10"},
        "spray_access": {"ip": "192.168.1.10"},
        "reestablish": {"ip": "192.168.1.10"},
        "escalate": {"ip": "192.168.1.10"},
        "takedown": {"ip": "192.168.1.10", "service": "nginx", "mode": "stop"},
        "block_port": {"ip": "192.168.1.10", "port": 3306, "display": "MySQL pin"},
        "rotate_credlist": {"ip": "192.168.1.10"},
        "lockout": {"ip": "192.168.1.10"},
        "persist": {"ip": "192.168.1.10", "family": "auto"},
        "implant_channel": {"ip": "192.168.1.10"},
        "task_channel": {"ip": "192.168.1.10", "command": "systemctl stop nginx"},
        "improvise": {"ip": "192.168.1.10", "command": "id"},
    }
    missing = [e for e in REGISTRY if e not in samples]
    assert missing == [], f"add a dry-run sample for: {missing}"
    # recon is the one effect the final window forbids (its own cutoff)
    clocks = {"recon": 0.2}
    for effect_id, params in samples.items():
        ssh = FakeSSH()
        ctx = _dry_run_ctx(tmp_path / effect_id, ssh=ssh, clock=clocks.get(effect_id, 0.99))
        result = execute(effect_id, dict(params), ctx)
        assert result.ok, f"{effect_id} dry-run failed: {result.detail}"
        assert ssh.calls == [], f"{effect_id} touched a box under dry-run"


def _dry_run_ctx(tmp_path, ssh, clock=0.99):
    from tests.helpers import DEFAULT_COMP, make_intel
    from tlaloc.c2 import FakeMythic
    from tlaloc.policy import Policy
    cfg = base_cfg(red={"assume_breach": "off"}, run={"host_guard": False})
    ctx = make_ctx(tmp_path, cfg=cfg, comp_dir=str(DEFAULT_COMP), ssh=ssh,
                   dry_run=True, compfile=False)
    ctx.c2 = FakeMythic({"web01-1": {"seen_at": ctx.now(), "agent": "poseidon"}})
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root",
                           escalation="sudo")
    ctx.world.add_foothold("192.168.1.20", "Administrator", "boxpw-1", "root",
                           windows=True)
    ctx.world.ensure_event_start()
    ctx.world.meta["event_start"] = ctx.now() - clock * ctx.cfg["red"]["duration_min"] * 60
    ctx.world.save()
    ctx.policy = Policy(ctx.cfg, ctx.world, ctx.intel, sensors=ctx.sensors, rng=ctx.rng)
    return ctx


# --- recon (reference effect) -------------------------------------------------

def test_recon_records_ports_and_kinds(tmp_path, monkeypatch):
    monkeypatch.setattr("tlaloc.effects.recon.shutil.which",
                        lambda name: f"/usr/bin/{name}")
    ssh = FakeSSH().on("nmap", rc=0, out=NMAP_XML)
    ctx = make_ctx(tmp_path, ssh=ssh)
    result = execute("recon", {"ip": "192.168.1.10"}, ctx)
    assert result.ok, result.detail
    target = ctx.world.target_record("192.168.1.10")
    assert target["recon_done"] is True
    assert {p["port"] for p in target["ports"]} == {22, 80, 3306}
    assert any("web" in note for note in target["notes"])


def test_recon_fails_honestly_without_nmap_or_output(tmp_path, monkeypatch):
    monkeypatch.setattr("tlaloc.effects.recon.shutil.which",
                        lambda name: f"/usr/bin/{name}")
    ctx = make_ctx(tmp_path, ssh=FakeSSH().on("nmap", rc="raise"))
    result = execute("recon", {"ip": "192.168.1.10"}, ctx)
    assert result.ok is False
    assert "nmap" in result.detail


def test_recon_is_blocked_by_the_cutoff(tmp_path):
    from tlaloc.policy import Policy
    ctx = make_ctx(tmp_path)
    policy = Policy(ctx.cfg, ctx.world, ctx.intel)
    ctx.policy = policy
    ctx.world.ensure_event_start()
    ctx.world.meta["event_start"] -= 0.95 * ctx.cfg["red"]["duration_min"] * 60
    ctx.world.save()
    result = execute("recon", {"ip": "192.168.1.10"}, ctx)
    assert result.ok is False and "cutoff" in result.detail


# --- takedown (reference effect) ---------------------------------------------

def _rooted_ctx(tmp_path, ssh, **cfg_over):
    ctx = make_ctx(tmp_path, ssh=ssh, **cfg_over)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root",
                           escalation="sudo")
    ctx.world.ensure_event_start()
    ctx.world.meta["event_start"] -= 0.25 * ctx.cfg["red"]["duration_min"] * 60
    ctx.world.save()
    from tlaloc.policy import Policy
    ctx.policy = Policy(ctx.cfg, ctx.world, ctx.intel, sensors=ctx.sensors)
    return ctx


def test_takedown_stop_records_impact_and_restore(tmp_path):
    ssh = FakeSSH()
    ssh.on("systemctl is-active nginx.service", rc=0, out="nginx\n")   # resolution
    ssh.on("systemctl stop nginx", rc=0, out="")
    ssh.on("systemctl is-active nginx", rc=0, out="inactive")
    ctx = _rooted_ctx(tmp_path, ssh)
    result = execute("takedown", {"ip": "192.168.1.10", "service": "nginx",
                                  "mode": "stop"}, ctx)
    assert result.ok, result.detail
    impacts = ctx.world.impacts()
    assert impacts and impacts[0]["service"] == "nginx" and impacts[0]["mode"] == "stop"
    assert ctx.world.restore_table()[0]["recipe"][0].startswith("systemctl unmask")


def test_takedown_reports_failure_when_the_service_stays_up(tmp_path):
    ssh = FakeSSH()
    ssh.on("systemctl stop", rc=0, out="")
    ssh.on("systemctl is-active", rc=0, out="active")
    ctx = _rooted_ctx(tmp_path, ssh)
    result = execute("takedown", {"ip": "192.168.1.10", "service": "nginx",
                                  "mode": "stop"}, ctx)
    assert result.ok is False and "still active" in result.detail
    assert ctx.world.impacts() == []


def test_takedown_hard_mode_that_does_not_hold_degrades_to_mask(tmp_path):
    ssh = FakeSSH()
    ssh.on("TLALOC_HARD", rc=0,
           out="TLALOC_HARD RENAME:/usr/sbin/nginx STATE=active")
    ctx = _rooted_ctx(tmp_path, ssh)
    ctx.world.meta["event_start"] = 0            # far into the event: brutal tier
    ctx.world.save()
    result = execute("takedown", {"ip": "192.168.1.10", "service": "nginx",
                                  "mode": "stop_rename_config"}, ctx)
    assert result.ok is False
    assert "did not hold" in result.detail
    assert any("mask" in cmd for cmd in ssh.sent("systemctl mask"))
    assert ctx.world.impacts() == [], "a takedown that did not hold is not an impact"


def test_takedown_refuses_our_own_ssh_path(tmp_path):
    ctx = _rooted_ctx(tmp_path, FakeSSH())
    result = execute("takedown", {"ip": "192.168.1.10", "service": "ssh",
                                  "mode": "stop"}, ctx)
    assert result.ok is False and "ssh" in result.detail


def test_takedown_windows_admin_path(tmp_path):
    ssh = FakeSSH()
    ssh.on("NetTCPConnection", rc=0, out="CLOSED")     # the :5985 listen check
    ctx = make_ctx(tmp_path, ssh=ssh)
    ctx.world.add_foothold("192.168.1.20", "Administrator", "boxpw-1", "root",
                           windows=True)
    ctx.world.ensure_event_start()
    ctx.world.meta["event_start"] -= 0.25 * ctx.cfg["red"]["duration_min"] * 60
    ctx.world.save()
    from tlaloc.policy import Policy
    ctx.policy = Policy(ctx.cfg, ctx.world, ctx.intel, sensors=ctx.sensors)
    result = execute("takedown", {"ip": "192.168.1.20", "service": "Enable WinRM",
                                  "mode": "stop"}, ctx)
    assert result.ok, result.detail
    assert ctx.world.impacts()[0]["service"] == "winrm"


def test_takedown_needs_root(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.world.add_foothold("192.168.1.10", "analyst", "pw", "user")
    from tlaloc.policy import Policy
    ctx.policy = Policy(ctx.cfg, ctx.world, ctx.intel, sensors=ctx.sensors)
    result = execute("takedown", {"ip": "192.168.1.10", "service": "nginx",
                                  "mode": "stop"}, ctx)
    assert result.ok is False and "root" in result.detail


# --- artifact names -----------------------------------------------------------

def test_names_come_from_the_pool_and_do_not_repeat(tmp_path):
    from tlaloc.effects import artifactnames as names
    ctx = make_ctx(tmp_path)
    first = names.linux_names(ctx.world, rng=ctx.rng, mode="full")
    second = names.linux_names(ctx.world, rng=ctx.rng, mode="full")
    assert first["unit"] != second["unit"]
    assert first["user"].startswith("svc-") and names.is_planted_account(first["user"])
    stem_mode = names.linux_names(ctx.world, rng=ctx.rng, mode="stem")
    suffix_mode = names.linux_names(ctx.world, rng=ctx.rng, mode="suffix")
    assert stem_mode["unit"].removesuffix(".service") in names.LINUX_STEMS
    assert suffix_mode["unit"].removesuffix(".service") in names.SUFFIX_SET
    windows = names.windows_names(ctx.world, rng=ctx.rng, mode="full")
    assert windows["task"][:1].isupper()


def test_names_never_leak_pool_words_into_descriptions(tmp_path):
    from tlaloc.effects import artifactnames as names
    ctx = make_ctx(tmp_path)
    drawn = names.linux_names(ctx.world, rng=ctx.rng, mode="stem")
    words = set(drawn["unit_desc"].lower().replace(".", " ").split())
    assert not (words & names.SUFFIX_SET), "a stem-mode plant must not leak a suffix"
