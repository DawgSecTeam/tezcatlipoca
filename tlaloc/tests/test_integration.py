"""End-to-end on fakes: the loop, the seed stage, and the report artifacts.

Nothing here touches a box or a model: the transport is scripted, clef is a stub,
C2 is in-memory. What is NOT faked is the wiring — the loop's decision path, the
effect template, the world state and the report artifacts are the production code.
"""

import json
import stat
from pathlib import Path

import pytest

from tests.helpers import DEFAULT_COMP, FakeC2, FakeSSH, base_cfg, make_ctx
from tlaloc import report as report_mod
from tlaloc import seed as seed_mod
from tlaloc.decide import Decider, StubClef
from tlaloc.effects import REGISTRY, Effect, Result, execute
from tlaloc.errors import GuardrailError, TlalocError
from tlaloc.policy import Policy
from tlaloc.run import Loop

FIXTURE = str(DEFAULT_COMP)


def live_ctx(tmp_path, ssh=None, c2=None, *, tier=0.25, breach="off", **cfg_over):
    cfg = base_cfg(red={"assume_breach": breach}, run={"host_guard": False},
                   **cfg_over)
    ctx = make_ctx(tmp_path, cfg=cfg, comp_dir=FIXTURE, ssh=ssh or FakeSSH(), c2=c2,
                   compfile=False)
    ctx.world.ensure_event_start()
    duration = ctx.cfg["red"]["duration_min"]
    ctx.world.meta["event_start"] = ctx.now() - tier * duration * 60
    ctx.world.save()
    policy = Policy(ctx.cfg, ctx.world, ctx.intel, sensors=ctx.sensors, rng=ctx.rng)
    ctx.policy = policy
    decider = Decider(ctx.cfg, ctx.world, ctx.log, clef=StubClef(), policy=policy)
    ctx.decider = decider
    return ctx, policy, decider


def scripted_takedown_ssh():
    ssh = FakeSSH()
    ssh.on("systemctl is-active nginx.service", rc=0, out="nginx\n")
    ssh.on("systemctl stop nginx", rc=0, out="")
    ssh.on("systemctl is-active nginx", rc=0, out="inactive")
    return ssh


# --- the loop -----------------------------------------------------------------

def test_loop_runs_cycles_through_the_gate_and_writes_reports(tmp_path):
    ssh = scripted_takedown_ssh()
    ctx, policy, decider = live_ctx(tmp_path, ssh=ssh)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root",
                           escalation="sudo")
    candidates = policy.candidates(ctx)
    takedown = next(c for c in candidates if c["effect"] == "takedown")
    decider.clef = StubClef(answers={
        "next_effect": {"type": "choice", "choice": takedown["id"],
                        "probabilities": {takedown["id"]: 0.95, "hold": 0.05}},
        "pressure_count": {"type": "score", "score": 0.0,
                           "probabilities": {"0": 0.9, "1": 0.1}},
        "escalate_now": {"type": "noul", "noul": 0.05}})

    loop = Loop(ctx, policy=policy, decider=decider, sensors=ctx.sensors,
                sleeper=lambda _s: None)
    outcome = loop.run(max_cycles=2)

    assert outcome["cycles"] == 2
    impacts = ctx.world.impacts()
    assert impacts and impacts[0]["service"] == "nginx"
    kinds = [e["kind"] for e in ctx.log.read()]
    assert "run_start" in kinds and "run_end" in kinds and "decision" in kinds
    reports = outcome["reports"]
    assert Path(reports["report"]).exists()
    assert Path(reports["restore_table"]).exists()
    assert "Restore table" in Path(reports["report"]).read_text()


def test_pressure_budget_falls_through_a_refused_candidate(tmp_path):
    """A protected candidate must not absorb the budget (live: ssh did every cycle)."""
    from tlaloc.decide import Decision
    from tlaloc.policy import make_candidate
    ctx, policy, decider = live_ctx(tmp_path)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root",
                           escalation="sudo")
    ssh_candidate = make_candidate("takedown", {"ip": "192.168.1.10",
                                                 "service": "ssh", "mode": "stop"},
                                   99, "our own access path")
    nginx_candidate = make_candidate("takedown", {"ip": "192.168.1.10",
                                                   "service": "nginx", "mode": "stop"},
                                     50, "a scored service")
    loop = Loop(ctx, policy=policy, decider=decider, sensors=ctx.sensors,
                sleeper=lambda _s: None)
    extra = loop._extra_pressure(1, [ssh_candidate, nginx_candidate],
                                 Decision("next_effect", "hold"))
    assert [holder.chosen["params"]["service"] for holder in extra] == ["nginx"]
    kinds = [event["kind"] for event in ctx.log.read()]
    assert "pressure_candidate_rejected" in kinds


def test_loop_halts_on_the_kill_switch(tmp_path):
    ctx, policy, decider = live_ctx(tmp_path, ssh=scripted_takedown_ssh())
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root")
    Path(ctx.cfg["run"]["kill_file"]).write_text("operator stop")
    loop = Loop(ctx, policy=policy, decider=decider, sensors=ctx.sensors,
                sleeper=lambda _s: None)
    with pytest.raises(GuardrailError):
        loop.run(max_cycles=1)


def test_loop_stops_after_consecutive_cycle_errors(tmp_path):
    ctx, policy, decider = live_ctx(tmp_path)
    ctx.cfg["run"]["error_halt_after"] = 2

    def boom(_ctx):
        raise RuntimeError("cycle bug")

    policy.candidates = boom
    loop = Loop(ctx, policy=policy, decider=decider, sensors=ctx.sensors,
                sleeper=lambda _s: None)
    outcome = loop.run(max_cycles=10)
    assert outcome["cycles"] <= 3
    kinds = [e["kind"] for e in ctx.log.read()]
    assert "halt_after_errors" in kinds
    errors = [e for e in ctx.log.read() if e["kind"] == "error"]
    assert errors and "cycle bug" in errors[0]["traceback"]


def test_loop_uses_the_weighted_mode_when_escalation_is_chosen(tmp_path):
    ssh = FakeSSH()
    ssh.on("systemctl is-active nginx.service", rc=0, out="nginx\n")
    ssh.on("systemctl stop nginx", rc=0, out="")
    ssh.on("systemctl mask nginx", rc=0, out="")
    ssh.on("systemctl is-active nginx", rc=0, out="inactive")
    # 60% of a 90-minute event: the medium tier, so stop_mask is legal
    ctx, policy, decider = live_ctx(tmp_path, ssh=ssh, tier=0.6)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root")
    candidates = policy.candidates(ctx)
    takedown = next(c for c in candidates if c["effect"] == "takedown")
    decider.clef = StubClef(answers={
        "next_effect": {"type": "choice", "choice": takedown["id"],
                        "probabilities": {takedown["id"]: 0.95}},
        "escalate_now": {"type": "noul", "noul": 0.95},
        "pressure_count": {"type": "score", "score": 0.0,
                           "probabilities": {"0": 0.9}}})
    loop = Loop(ctx, policy=policy, decider=decider, sensors=ctx.sensors,
                sleeper=lambda _s: None)
    loop.run(max_cycles=1)
    assert ctx.world.impacts(), "an impact must be recorded"
    assert ctx.world.impacts()[0]["mode"] in ("stop", "stop_mask")
    assert any("mask" in cmd for cmd in ssh.sent("systemctl mask")) or \
        ctx.world.impacts()[0]["mode"] == "stop"


def test_loop_refuses_to_run_off_the_red_vm(tmp_path):
    cfg = base_cfg(run={"host_guard": True}, red={"assume_breach": "off"})
    ctx = make_ctx(tmp_path, cfg=cfg, comp_dir=FIXTURE, compfile=False)
    decider = Decider(ctx.cfg, ctx.world, ctx.log, clef=StubClef(),
                      policy=Policy(ctx.cfg, ctx.world, ctx.intel))
    loop = Loop(ctx, decider=decider)
    with pytest.raises(TlalocError) as excinfo:
        loop.guard_host()
    assert "red01" in str(excinfo.value)


# --- seed ---------------------------------------------------------------------

class _FakeEffect(Effect):
    params = {"ip": {"type": "str", "required": True},
              "family": {"type": "str"}}

    def run(self, ctx, params):
        ip = params["ip"]
        self.calls.append(params)
        if params.get("family") in ("preload",):
            return Result(False, "no staged .so")
        return Result(True, f"{self.id} on {ip}",
                      artifacts=[{"ip": ip, "kind": f"{self.id}-x",
                                  "detail": f"{self.id} planted on {ip}",
                                  "paths": [f"/etc/{self.id}.x"]}],
                      restore={"recipe": [f"undo {self.id}"], "secrets": None})


def _fake_effects():
    # Load the real catalogue first: make_ctx() reloads it, and an import-order-dependent
    # overwrite would replace these fakes with the real effects mid-test.
    from tlaloc.effects import reload_registry
    reload_registry()
    made = {}
    for effect_id in ("spray_access", "escalate", "persist"):
        effect = _FakeEffect()
        effect.id = effect_id
        effect.calls = []
        made[effect_id] = effect
    return made


def test_seed_off_reports_zero_steps_for_the_cli(tmp_path):
    ctx, _policy, _decider = live_ctx(tmp_path, breach="off")
    outcome = seed_mod.seed_range(ctx)
    assert outcome["mode"] == "off" and outcome["steps"] == 0


def test_seed_full_walks_every_stage_and_writes_the_manifest(tmp_path):
    fakes = _fake_effects()
    saved = {k: REGISTRY.get(k) for k in fakes}
    REGISTRY.update(fakes)
    try:
        ssh = FakeSSH().on("is-active", rc=0, out="active")
        ctx, policy, decider = live_ctx(tmp_path, ssh=ssh, c2=FakeC2({}),
                                       breach="full")
        _manifest(tmp_path)
        outcome = seed_mod.seed_range(ctx, depth=3, teams=["Blue1"])
        assert outcome["mode"] == "full"
        records = outcome["boxes"]
        assert records, "every planned box must be recorded"
        first = next(iter(records.values()))
        assert "access" in first["stages"] and "escalate" in first["stages"]
        assert ctx.world.seed_done() is True
        manifest = json.loads(Path(outcome["manifest"]["path"]).read_text())
        assert manifest["assume_breach"] == "full"
        assert set(manifest["boxes"]), "the manifest must list what is planted where"
        assert all("restore" in box for box in manifest["boxes"].values())
    finally:
        for key, value in saved.items():
            if value is None:
                REGISTRY.pop(key, None)
            else:
                REGISTRY[key] = value


def test_seed_is_resumable_and_respects_light(tmp_path):
    fakes = _fake_effects()
    saved = {k: REGISTRY.get(k) for k in fakes}
    REGISTRY.update(fakes)
    try:
        ctx, policy, decider = live_ctx(tmp_path, c2=FakeC2({}), breach="full")
        _manifest(tmp_path)
        first = seed_mod.seed_range(ctx, depth=1, teams=["Blue1"])
        assert first["steps"] > 0
        ctx.world.set_seed_done(False)
        second = seed_mod.seed_range(ctx, depth=1, teams=["Blue1"])
        assert second["steps"] == 0, "a completed stage must not be redone"
        ctx.world.set_seed_done(False)
        light_ctx, _, _ = live_ctx(tmp_path / "light", ssh=FakeSSH().on("is-active",
                                                                       rc=0, out="active"),
                                   c2=FakeC2({}), breach="light")
        _manifest(tmp_path / "light")
        light_ctx.cfg["run"]["comp_dir"] = str(tmp_path / "light")
        light = seed_mod.seed_range(light_ctx, depth=3, teams=["Blue1"])
        assert "persist" not in json.dumps(light["boxes"]), \
            "assume_breach light stops at the channel"
    finally:
        for key, value in saved.items():
            if value is None:
                REGISTRY.pop(key, None)
            else:
                REGISTRY[key] = value


def test_a_stale_seed_done_marker_does_not_skip_outstanding_stages(tmp_path):
    """The old seed set seed_done even when every stage failed; a re-seed then did
    nothing and the deploy's planted-box guard saw an empty manifest forever."""
    fakes = _fake_effects()
    saved = {k: REGISTRY.get(k) for k in fakes}
    REGISTRY.update(fakes)
    try:
        ctx, policy, decider = live_ctx(tmp_path, c2=FakeC2({}), breach="full")
        _manifest(tmp_path)
        ctx.world.set_seed_done(True)          # as the broken run left it
        outcome = seed_mod.seed_range(ctx, depth=1, teams=["Blue1"],
                                      manifest_dir=tmp_path / "red")
        assert outcome["steps"] > 0, "outstanding stages must be retried"
        assert ctx.world.seed_done() is True
    finally:
        for key, value in saved.items():
            if value is None:
                REGISTRY.pop(key, None)
            else:
                REGISTRY[key] = value


def test_seed_off_does_nothing(tmp_path):
    ctx, policy, decider = live_ctx(tmp_path, breach="off")
    outcome = seed_mod.seed_range(ctx)
    assert outcome["mode"] == "off" and outcome["boxes"] == {}


def _manifest(tmp_path):
    payload_dir = tmp_path / "red" / "payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    for box, os_name in (("web01-1", "linux"), ("db01-1", "linux"),
                         ("dc01-1", "windows")):
        (payload_dir / f"{box}.bin").write_bytes(b"\x7fELF fake")
    manifest = {"comp": "comp-min", "operation": "comp-min", "transports": ["http"],
                "boxes": {f"{b}": {"uuid": f"u-{b}", "file": f"{b}.bin",
                                   "os": o, "agent": "poseidon", "transport": "http"}
                          for b, o in (("web01-1", "linux"), ("db01-1", "linux"),
                                       ("dc01-1", "windows"))}}
    (payload_dir / "manifest.json").write_text(json.dumps(manifest))
    return manifest


# --- reports ------------------------------------------------------------------

def test_report_artifacts_have_the_right_modes_and_contents(tmp_path):
    ctx, policy, decider = live_ctx(tmp_path)
    ctx.world.add_impact("192.168.1.10", "nginx", "stop_mask")
    ctx.world.add_restore("takedown", "192.168.1.10", "nginx",
                          ["systemctl unmask nginx", "systemctl start nginx"])
    ctx.world.add_sabotage("192.168.1.10", "svc-backup", "old-pw", "new-pw")
    paths = report_mod.write_artifacts(tmp_path, ctx.world, policy=policy,
                                       sensors=ctx.sensors)
    report = Path(paths["report"]).read_text()
    secrets = Path(paths["secrets"]).read_text()
    table = json.loads(Path(paths["restore_table"]).read_text())
    assert "nginx" in report and "old-pw" not in report and "new-pw" not in report
    assert "old-pw" in secrets and "new-pw" in secrets
    assert stat.S_IMODE(Path(paths["secrets"]).stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(paths["restore_table"]).stat().st_mode) == 0o600
    assert table["entries"][0]["effect"] == "takedown"
    assert table["commands"][0]["command"] == "systemctl unmask nginx"
    assert table["summary"]["impacts"] == 1


def test_report_narrative_is_passed_through_verbatim(tmp_path):
    ctx, policy, decider = live_ctx(tmp_path)
    paths = report_mod.write_artifacts(tmp_path, ctx.world, policy=policy,
                                       narrative="Red held the range all event.")
    assert "Red held the range all event." in Path(paths["report"]).read_text()


def test_the_pressure_budget_offers_more_than_one_candidate_per_count(tmp_path):
    """A refused target must not absorb the budget: the list carries fallbacks."""
    from tlaloc.decide import Decision
    from tlaloc.run import PRESSURE_FALLTHROUGH
    ctx, policy, decider = live_ctx(tmp_path, ssh=scripted_takedown_ssh())
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root",
                           escalation="sudo")
    loop = Loop(ctx, policy=policy, decider=decider, sensors=ctx.sensors,
                sleeper=lambda _s: None)
    candidates = policy.candidates(ctx)
    eligible = [c for c in candidates
                if c["effect"] in ("takedown", "block_port")]
    holder = Decision("next_effect", None, source="deterministic", chosen=None)
    out = loop._extra_pressure(1, candidates, holder)
    # The preflight drops the refused ssh candidate, so the list is the legal
    # fallbacks (bounded), not the raw candidate count.
    assert out, "a legal pressure target must remain"
    assert all("ssh" not in (c.chosen or {}).get("id", "") for c in out)
    assert len(out) <= min(len(eligible), 1 + PRESSURE_FALLTHROUGH)


def test_artifact_health_logs_a_dead_artifact_without_crashing(tmp_path):
    ssh = FakeSSH().on("is-active", out="inactive")
    ctx, policy, decider = live_ctx(tmp_path, ssh=ssh)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root",
                           escalation="sudo")
    ctx.world.add_artifact("192.168.1.10", "persist-cron", "cron (/etc/cron.d/x)",
                           check_cmd="systemctl is-active x")
    loop = Loop(ctx, policy=policy, decider=decider, sensors=ctx.sensors,
                sleeper=lambda _s: None)
    loop.verify_artifacts()
    assert any(e["kind"] == "artifact_dead" for e in ctx.log.read())


def test_a_policy_refusal_does_not_count_as_an_effect_failure(tmp_path):
    """A protected target is not a broken mechanism; retry budgets must not eat it."""
    ctx, policy, decider = live_ctx(tmp_path)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root",
                           escalation="sudo")
    result = execute("takedown", {"ip": "192.168.1.10", "service": "ssh"}, ctx)
    assert result.ok is False
    assert ctx.world.failure_count("takedown", "192.168.1.10", "ssh") == 0


def test_two_failures_on_one_service_do_not_block_its_siblings(tmp_path):
    """A hardened unit exhausts only its own retry budget, not the box's."""
    ctx, policy, decider = live_ctx(tmp_path, ssh=scripted_takedown_ssh())
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root",
                           escalation="sudo")
    ctx.world.tally_failure("takedown", "192.168.1.10", "mysql")
    ctx.world.tally_failure("takedown", "192.168.1.10", "mysql")
    result = execute("takedown", {"ip": "192.168.1.10", "service": "nginx",
                                  "mode": "stop"}, ctx)
    assert result.ok, result.detail
    assert result.impact["service"] == "nginx"


def test_an_impact_carries_the_team_the_report_groups_by(tmp_path):
    """The report groups impacts by team; reading only the target record left it "?"."""
    ctx, policy, decider = live_ctx(tmp_path, ssh=scripted_takedown_ssh())
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root",
                           escalation="sudo")
    result = execute("takedown", {"ip": "192.168.1.10", "service": "nginx",
                                  "mode": "stop"}, ctx)
    assert result.ok, result.detail
    impact = ctx.world.impacts()[-1]
    assert impact["team"] == ctx.intel.team_of("192.168.1.10")
    assert impact["team"]
