"""The C2 layer: Mythic REST mapping, callback freshness, planting, tasking.

Every test drives the client through its injected `http` seam, so nothing here
touches a network — which is exactly what the plan asks for: the C2 path has to
be verifiable offline because the live Mythic only exists during a comp.
"""

import json
import shutil
from pathlib import Path

import pytest

from tests.helpers import FakeSSH, base_cfg, make_ctx
from tlaloc.c2 import FakeMythic
from tlaloc.c2.implant import Planter, load_manifest, payload_file
from tlaloc.c2.mythic import MythicClient, _epoch
from tlaloc.errors import C2Error


class Seq(list):
    """A queue of responses: each call pops the next one."""


class StubHTTP:
    """A scripted Mythic: routes -> response, and records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, path, body, timeout):
        self.calls.append({"method": method, "path": path, "body": body})
        for (route_method, prefix), response in self.routes.items():
            if route_method == method and path.startswith(prefix):
                if isinstance(response, Seq):
                    return response.pop(0)
                return response
        raise C2Error(f"unrouted {method} {path}")


def c2_cfg(**over):
    cfg = base_cfg(c2={"enabled": True, "base_url": "https://mythic.example",
                       "username": "tlaloc", "operation": "comp-x",
                       "verify_tls": False, "callback_max_age_sec": 120})
    cfg["c2"].update(over)
    return cfg


def test_epoch_parses_mythic_timestamps():
    assert _epoch("2026-10-16T02:00:00Z") == _epoch("2026-10-16T02:00:00+00:00")
    assert _epoch(1770000000) == 1770000000.0
    assert _epoch(None) is None and _epoch("nonsense") is None


def test_callbacks_map_description_to_box_and_keep_freshness():
    now = 1792116000.0        # 2026-10-16T02:00:00Z, just after both check-ins
    http = StubHTTP({("GET", "/api/v1.4/callbacks/"): [
        {"id": 7, "display_id": 7, "description": "web01-1", "host": "192.168.1.10",
         "user": "ubuntu", "agent": "poseidon", "last_checkin": "2026-10-16T01:59:30Z"},
        {"id": 8, "display_id": 8, "description": "db01-1", "host": "192.168.1.11",
         "user": "ubuntu", "agent": "poseidon", "last_checkin": "2026-10-16T01:00:00Z"},
    ]})
    client = MythicClient(c2_cfg(), http=http, now=lambda: now)
    callbacks = client.callbacks()
    assert set(callbacks) == {"web01-1", "db01-1"}
    assert callbacks["web01-1"]["agent"] == "poseidon"
    fresh = client.fresh_callbacks()
    assert "web01-1" in fresh and "db01-1" not in fresh


def test_callback_row_without_a_timestamp_is_not_fresh():
    now = 1770000000.0
    http = StubHTTP({("GET", "/api/v1.4/callbacks/"): [
        {"id": 1, "description": "web01-1", "agent": "poseidon",
         "last_checkin": None}]})
    client = MythicClient(c2_cfg(), http=http, now=lambda: now)
    assert client.callbacks()["web01-1"]["stale"] is True
    assert client.fresh_callbacks() == {}


def test_tasking_creates_and_polls_a_task():
    http = StubHTTP({
        ("GET", "/api/v1.4/callbacks/"): [
            {"id": 42, "description": "web01-1", "last_checkin": "2026-10-16T01:59:30Z"}],
        ("POST", "/api/v1.4/task/"): {"id": 900},
        ("GET", "/api/v1.4/task/900/"): Seq([
            {"id": 900, "status": "submitted"},
            {"id": 900, "status": "completed",
             "response": json.dumps({"output": "nginx stopped"})}]),
    })
    client = MythicClient(c2_cfg(), http=http, now=lambda: 1770000000.0)
    outcome = client.task("web01-1", "systemctl stop nginx", timeout=5,
                          poll_interval=0)
    assert outcome["ok"] is True and outcome["output"] == "nginx stopped"
    posted = [c for c in http.calls if c["method"] == "POST"][0]
    assert posted["body"] == {"callback_id": 42, "command": "systemctl stop nginx"}


def test_tasking_reports_a_missing_callback_instead_of_guessing():
    http = StubHTTP({("GET", "/api/v1.4/callbacks/"): []})
    client = MythicClient(c2_cfg(), http=http, now=lambda: 1770000000.0)
    outcome = client.task("web01-1", "id")
    assert outcome["ok"] is False and "no callback" in outcome["detail"]


def test_a_failed_task_is_not_success():
    http = StubHTTP({
        ("GET", "/api/v1.4/callbacks/"): [{"id": 1, "description": "web01-1",
                                           "last_checkin": "2026-10-16T01:59:30Z"}],
        ("POST", "/api/v1.4/task/"): {"id": 5},
        ("GET", "/api/v1.4/task/5/"): Seq([{"id": 5, "status": "error",
                                            "response": json.dumps({"output": "denied"})}]),
    })
    client = MythicClient(c2_cfg(), http=http, now=lambda: 1770000000.0)
    outcome = client.task("web01-1", "id", timeout=1, poll_interval=0)
    assert outcome["ok"] is False and outcome["detail"].startswith("task error")


def test_disabled_c2_refuses_to_task():
    cfg = c2_cfg(enabled=False)
    client = MythicClient(cfg, http=StubHTTP({}), now=lambda: 0.0)
    with pytest.raises(C2Error):
        client.task("web01-1", "id")


def test_login_is_cached_on_the_client():
    http = StubHTTP({("POST", "/auth"): {"access_token": "tok"}})
    client = MythicClient(c2_cfg(), http=http, now=lambda: 0.0)
    assert client.login() == "tok"
    assert client._token == "tok"


# --- planting -----------------------------------------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "comp-min"
INTEL_FILES = ("teams.json", "boxes.json", "users.json", "box_services.json",
               "box_vulns.json", "nakon-config.json", ".deploy_state.json")


def _manifest(tmp_path, box_id="web01-1"):
    # The gate's scope comes from intel, so the fixture's team files travel with
    # the manifest — a box outside a known subnet is refused (fail closed).
    for name in INTEL_FILES:
        shutil.copy(FIXTURE / name, tmp_path / name)
    payload_dir = tmp_path / "red" / "payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload = payload_dir / f"{box_id}.bin"
    payload.write_bytes(b"\x7fELF fake")
    manifest = {"comp": "comp-x", "operation": "comp-x", "transports": ["http"],
                "boxes": {box_id: {"uuid": "u-1", "file": f"{box_id}.bin",
                                   "os": "linux", "agent": "poseidon",
                                   "transport": "http"}}}
    (payload_dir / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_manifest_is_loaded_and_payload_located(tmp_path):
    manifest = _manifest(tmp_path)
    loaded = load_manifest(tmp_path)
    assert loaded["boxes"]["web01-1"]["uuid"] == "u-1"
    assert payload_file(tmp_path, loaded["boxes"]["web01-1"]).exists()
    assert payload_file(tmp_path, {"file": "nope.bin"}) is None


def test_planter_installs_and_reports_the_callback(tmp_path):
    _manifest(tmp_path)
    ssh = FakeSSH()
    ssh.on("is-active", rc=0, out="active")
    ctx = make_ctx(tmp_path / "state", ssh=ssh)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root")
    ctx.c2 = FakeMythic({"web01-1": {"seen_at": ctx.now(), "agent": "poseidon",
                                     "external_id": "7"}})
    planter = Planter(ctx, comp_dir=tmp_path)
    info = planter.plant("192.168.1.10", "web01-1", wait_sec=0)
    assert info["ok"] is True and info["callback_seen"] is True
    assert any("systemctl enable" in (call["cmd"] or "") for call in ssh.calls)
    assert ctx.world.callback_age("web01-1") is not None


def test_planter_records_the_install_even_without_a_callback(tmp_path):
    _manifest(tmp_path)
    ssh = FakeSSH()
    ssh.on("is-active", rc=0, out="active")
    ctx = make_ctx(tmp_path / "state", ssh=ssh)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root")
    ctx.c2 = FakeMythic({})                      # no callback ever
    planter = Planter(ctx, comp_dir=tmp_path)
    info = planter.plant("192.168.1.10", "web01-1", wait_sec=0)
    assert info["ok"] is True and info["callback_seen"] is False
    assert "no callback" in json.dumps(info) or info["callback_seen"] is False


def test_planter_refuses_without_a_manifest_entry(tmp_path):
    _manifest(tmp_path)
    ctx = make_ctx(tmp_path / "state")
    ctx.c2 = FakeMythic({})
    planter = Planter(ctx, comp_dir=tmp_path)
    info = planter.plant("192.168.1.11", "db01-1", wait_sec=0)
    assert info["ok"] is False and "manifest" in info["detail"]


def test_implant_channel_effect_needs_the_callback_to_succeed(tmp_path):
    _manifest(tmp_path)
    ssh = FakeSSH().on("is-active", rc=0, out="active")
    # comp_dir points at the manifest; box_id is named explicitly because the
    # manifest's keys are comp-prep's, not something the agent may guess
    ctx = make_ctx(tmp_path / "state", ssh=ssh, comp_dir=tmp_path)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root")
    ctx.c2 = FakeMythic({})
    from tlaloc.effects import execute
    result = execute("implant_channel", {"ip": "192.168.1.10", "box_id": "web01-1"},
                     ctx)
    assert result.ok is False
    assert "no callback" in result.detail
    assert ctx.world.artifacts()[0]["kind"] == "c2-implant", \
        "the install is recorded even when the channel never checked in"


def test_task_channel_attributes_an_impact_only_when_classified(tmp_path):
    ssh = FakeSSH()
    ctx = make_ctx(tmp_path / "state", ssh=ssh)
    ctx.c2 = FakeMythic({"web01-1": {"seen_at": ctx.now(), "agent": "poseidon"}})
    from tlaloc.effects import execute
    result = execute("task_channel", {"ip": "192.168.1.10",
                                      "command": "systemctl stop nginx",
                                      "service": "nginx"}, ctx)
    assert result.ok
    # no decider attached -> intent unknown -> no impact row (never a guess)
    assert ctx.world.impacts() == []
    assert ctx.c2.tasks and ctx.c2.tasks[0]["command"] == "systemctl stop nginx"


def test_task_channel_rejects_a_box_with_no_live_channel(tmp_path):
    ctx = make_ctx(tmp_path / "state")
    ctx.c2 = FakeMythic({"web01-1": {"seen_at": ctx.now() - 9999, "agent": "poseidon"}})
    from tlaloc.effects import execute
    result = execute("task_channel", {"ip": "192.168.1.10", "command": "id"}, ctx)
    assert result.ok is False and "live callback" in result.detail


class StubDecider:
    """The decision layer's command_intent answer, without a model."""

    def __init__(self, intent="takedown", source="clef", prob=0.92):
        self.intent, self.source, self.prob = intent, source, prob

    def classify_command(self, command, os_name="linux"):
        from tlaloc.decide import Decision
        return Decision("command_intent", self.intent,
                        probabilities={self.intent: self.prob}, top_prob=self.prob,
                        source=self.source)


def test_task_channel_records_the_undo_for_a_tasked_command(tmp_path):
    """A C2-tasked command is a mutation: it must carry its undo."""
    ctx = make_ctx(tmp_path / "state")
    ctx.c2 = FakeMythic({"web01-1": {"seen_at": ctx.now(), "agent": "poseidon"}})
    from tlaloc.effects import execute
    result = execute("task_channel", {"ip": "192.168.1.10",
                                      "command": "mv /etc/nginx/nginx.conf /tmp/x"}, ctx)
    assert result.ok
    table = ctx.world.restore_table()
    assert len(table) == 1, "the restore table must not lose a C2-tasked mutation"
    assert table[0]["effect"] == "task_channel"
    assert any("mv /etc/nginx/nginx.conf /tmp/x" in str(step)
               for step in table[0]["recipe"])


def test_task_channel_undo_names_the_service_it_took_down(tmp_path):
    ctx = make_ctx(tmp_path / "state")
    ctx.c2 = FakeMythic({"web01-1": {"seen_at": ctx.now(), "agent": "poseidon"}})
    ctx.decider = StubDecider(intent="takedown")
    from tlaloc.effects import execute
    result = execute("task_channel", {"ip": "192.168.1.10",
                                      "command": "systemctl stop nginx",
                                      "service": "nginx"}, ctx)
    assert result.ok and result.impact["service"] == "nginx"
    assert ctx.world.restore_table()[0]["recipe"][0] == "systemctl start nginx.service"
    assert ctx.world.restore_table()[0].get("secrets") is None


def test_a_task_that_ran_but_failed_still_records_its_undo(tmp_path):
    """ok=False with the box touched is exactly the case a record gated on a
    proof would lose (the earlier agent's implant did)."""
    class FailingC2(FakeMythic):
        def task(self, box_id, command, timeout=120):
            return {"ok": False, "detail": "task error", "output": "",
                    "task_id": 77}

    ctx = make_ctx(tmp_path / "state")
    ctx.c2 = FailingC2({"web01-1": {"seen_at": ctx.now(), "agent": "poseidon"}})
    from tlaloc.effects import execute
    result = execute("task_channel", {"ip": "192.168.1.10",
                                      "command": "systemctl stop nginx",
                                      "service": "nginx"}, ctx)
    assert result.ok is False
    table = ctx.world.restore_table()
    assert len(table) == 1 and table[0]["recipe"][0] == "systemctl start nginx.service"


def test_build_payload_carries_the_callback_address():
    http = StubHTTP({("POST", "/api/v1.4/payload/"): {"uuid": "uuid-1"}})
    client = MythicClient(c2_cfg(callback_host="http://172.31.2.1",
                                 callback_port=8000), http=http)
    entry = client.build_payload("web01-1", "linux", transport="http")
    posted = [c for c in http.calls if c["method"] == "POST"][0]
    profile = posted["body"]["c2_profiles"][0]
    assert profile["c2_profile"] == "http"
    assert profile["c2_profile_parameters"] == {"callback_host": "http://172.31.2.1",
                                                "callback_port": 8000}
    assert entry["uuid"] == "uuid-1" and entry["callback"] == {"host": "http://172.31.2.1",
                                                               "port": 8000}
    assert posted["body"]["build_parameters"] == {}


def test_build_payload_without_a_callback_keeps_the_profile_default():
    """No configured callback = the profile instance's own value stays in place;
    nothing is invented (and the field is absent, not empty)."""
    http = StubHTTP({("POST", "/api/v1.4/payload/"): {"uuid": "uuid-2"}})
    client = MythicClient(c2_cfg(), http=http)
    client.build_payload("web01-1", "windows")
    posted = [c for c in http.calls if c["method"] == "POST"][0]
    profile = posted["body"]["c2_profiles"][0]
    assert "c2_profile_parameters" not in profile
    assert posted["body"]["payload_type"] == "apollo"
