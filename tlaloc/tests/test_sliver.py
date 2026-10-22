"""Sliver C2: transport matrix, callback attribution and the client contract."""

import json
from pathlib import Path
from types import SimpleNamespace

from tlaloc.c2.sliver import (DEFAULT_PORTS, SliverClient, _box_id,
                              _transport_of)


def _cfg(**over):
    c2 = {"enabled": True, "engine": "sliver", "base_url": "https://10.0.0.50:31337",
          "operator_config": "/etc/tlaloc/sliver.cfg",
          "dns_domain": "c2.demo.range", "transports": {},
          "callback_max_age_sec": 120, "task_timeout_sec": 60,
          "timeout_sec": 30}
    c2.update(over)
    return {"c2": c2}


def test_every_transport_url_targets_the_team_gateway():
    c = SliverClient(_cfg(), channel=object())
    assert c.c2_url("mtls", gateway="192.168.150.1") == "mtls://192.168.150.1:8888"
    assert c.c2_url("http", gateway="192.168.150.1") == "http://192.168.150.1:8080"
    assert c.c2_url("https", gateway="192.168.150.1") == "https://192.168.150.1:8443"
    assert c.c2_url("dns", gateway="192.168.150.1") == (
        "dns://c2.demo.range?resolvers=192.168.150.1")
    assert c.c2_url("wg", gateway="192.168.150.1") == "wg://192.168.150.1:51820"
    assert c.c2_url("tcp-pivot", pivot_host="192.168.150.4") == (
        "tcppivot://192.168.150.4:9898")
    assert c.c2_url("named-pipe", box_id="web01-150") == (
        "namedpipe:///pipe/sliver-web01-150")


def test_build_name_carries_the_box_and_transport_back_to_the_callback():
    assert _box_id("web01-150-mtls") == "web01-150"
    assert _box_id("web01-150-named-pipe-beacon") == "web01-150"
    assert _transport_of("web01-150-https") == "https"
    assert _transport_of("web01-150-tcp-pivot-beacon") == "tcp-pivot"


class FakeRpc:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def unary_unary(self, method, request_serializer, response_deserializer):
        def call(request, metadata=(), timeout=None):
            from tlaloc.c2.sliver_proto.commonpb import common_pb2
            from tlaloc.c2.sliver_proto.clientpb import client_pb2
            if method.endswith("GetSessions"):
                return client_pb2.Sessions(Sessions=[
                    row for row, beacon in self.rows if not beacon])
            if method.endswith("GetBeacons"):
                return client_pb2.Beacons(Beacons=[
                    row for row, beacon in self.rows if beacon])
            return common_pb2.Empty()
        return call


def test_callbacks_are_keyed_by_box_and_match_their_build_name():
    from tlaloc.c2.sliver_proto.clientpb import client_pb2
    row = client_pb2.Session(ID="id-1", Name="web01-150-https",
                             Hostname="web01", Username="root", PID=7,
                             OS="linux", Transport="https", Filename="/x",
                             LastCheckin=1000)
    c = SliverClient(_cfg(), channel=FakeRpc([(row, False)]), now=lambda: 1001)
    record = c.callbacks()["web01-150"]
    assert record["name"] == "web01-150-https"
    assert record["transport"] == "https" and record["async"] is False
    assert record["external_id"] == "id-1"


def test_each_transport_keeps_its_own_callback_row_on_one_box():
    from tlaloc.c2.sliver_proto.clientpb import client_pb2
    rows = [(client_pb2.Session(ID=f"id-{t}", Name=f"web01-150-{t}",
                                OS="linux", Transport=t, LastCheckin=1000 + i),
             False)
            for i, t in enumerate(("mtls", "http", "dns"))]
    c = SliverClient(_cfg(), channel=FakeRpc(rows), now=lambda: 1001)
    assert set(c.callbacks_by_name()) == {
        "web01-150-mtls", "web01-150-http", "web01-150-dns"}
    # the per-box view stays one freshest channel; the per-name view proves coverage
    assert c.callbacks()["web01-150"]["transport"] == "dns"


def test_dns_keeps_the_gateway_port_separate_from_the_server_port():
    assert DEFAULT_PORTS["dns"]["gateway"] == 53
    assert DEFAULT_PORTS["dns"]["server"] == 5353


def test_channels_up_is_a_replant_skip_only_when_every_transport_checked_in(tmp_path):
    from tlaloc.c2.implant import Planter
    from tlaloc.c2.sliver import FakeSliver
    manifest = {"boxes": {
        "web01-150-mtls": {"name": "web01-150-mtls", "box": "web01-150",
                           "transport": "mtls", "plant": True},
        "web01-150-http": {"name": "web01-150-http", "box": "web01-150",
                           "transport": "http", "plant": True},
        "web01-150-dns": {"name": "web01-150-dns", "box": "web01-150",
                          "transport": "dns", "plant": False},
    }}
    callbacks = {"web01-150-mtls": {"name": "web01-150-mtls", "seen_at": 100,
                                    "agent": "mtls", "async": False},
                 "web01-150-http": {"name": "web01-150-http", "seen_at": 100,
                                    "agent": "http", "async": False}}
    ctx = SimpleNamespace(cfg={"c2": {"implant_spread": "full",
                                      "callback_max_age_sec": 120},
                               "run": {"comp_dir": str(tmp_path)}},
                          world=SimpleNamespace(note_callback=lambda *a, **k: None),
                          c2=FakeSliver(callbacks))
    planter = Planter(ctx, comp_dir=tmp_path, manifest=manifest, now=lambda: 100)
    assert planter.channels_up("web01-150") is True
    planter.ctx.c2 = FakeSliver({"web01-150-mtls": callbacks["web01-150-mtls"]})
    assert planter.channels_up("web01-150") is False


class FakePrepClient:
    def __init__(self):
        self.builds = []

    def ensure_listeners(self):
        return {"started": []}

    def build_payload(self, box_id, os_name="linux", transport="mtls",
                      gateway=None, domain=None, pivot_host=None, beacon=False,
                      debug=False):
        name = f"{box_id}-{transport}" + ("-beacon" if beacon else "")
        self.builds.append((box_id, transport, beacon))
        return {"uuid": f"u-{name}", "name": name, "box": box_id, "os": os_name,
                "transport": transport,
                "kind": "beacon" if beacon else "session",
                "callback": "x", "data": b"payload", "size": 7,
                "sha256": "0" * 64}


def test_the_payload_matrix_covers_every_transport_and_builds_beacons(tmp_path):
    from tlaloc import cli
    boxes = [
        {"box": "web01", "identifier": "150", "ip": "192.168.150.4",
         "windows": False},
        {"box": "win02", "identifier": "150", "ip": "192.168.150.3",
         "windows": True},
    ]
    ctx = SimpleNamespace(intel=SimpleNamespace(boxes=lambda: boxes))
    client = FakePrepClient()
    manifest = {"boxes": {}}
    cli._prep_sliver(ctx, client, Path(tmp_path), manifest, list(cli._transports_arg("all")))
    entries = manifest["boxes"].values()
    transports = {entry["transport"] for entry in entries}
    assert {"mtls", "http", "https", "dns", "wg", "tcp-pivot"} <= transports
    assert "named-pipe" in transports
    assert any(entry["kind"] == "beacon" for entry in entries)
    assert all(entry["plant"] is False for entry in entries
               if entry["transport"] in ("named-pipe", "tcp-pivot")
               or entry["kind"] == "beacon")
    assert any(entry["plant"] is True for entry in entries)
    # One file per entry, and the payload bytes never ride the JSON manifest.
    for entry in entries:
        assert (Path(tmp_path) / entry["file"]).exists()
        assert "data" not in entry
    json.dumps(manifest)
