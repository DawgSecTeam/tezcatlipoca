import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from red import c3rberus_ops  # noqa: E402


def comp(tmp_path, teams, extra=""):
    d = tmp_path / "cde-x"
    d.mkdir(exist_ok=True)
    (d / "Compfile").write_text(textwrap.dedent(f"""\
        name cde-x
        domain_prefix mira-
        domain_suffix .corp.sus
        {extra}
    """))
    (d / "boxes.json").write_text(json.dumps([
        {"name": "ad01", "last_octet": 2, "template": "base-windows-server"},
        {"name": "web01", "last_octet": 4, "template": "base-fedora44-fix"},
        {"name": "db01", "last_octet": 5, "template": "base-ubuntu24.04"},
        {"name": "fw01", "last_octet": 1, "template": "pfsense-provision", "in_path": True}]))
    (d / "domain_roles.json").write_text(json.dumps({"ad01": "dc"}))
    (d / "users.json").write_text(json.dumps({"box_username": "blueteam"}))
    (d / "teams.json").write_text(json.dumps(
        {f"team{i}": {"identifier": tid} for i, tid in enumerate(teams, 1)}))
    return d


def test_selected(tmp_path):
    assert c3rberus_ops.selected(comp(tmp_path, [125], extra="c3rberus 1"))
    assert not c3rberus_ops.selected(comp(tmp_path, [125]))


def test_render_inventory(tmp_path):
    import yaml
    c = comp(tmp_path, [125, 126])
    doc = yaml.safe_load(c3rberus_ops.render_inventory(c, "SECRETpw", red_ip="10.0.0.247"))
    assert doc["c2_settings"]["c2_ip"] == "10.0.0.247"
    assert doc["c2_settings"]["script_server_url"] == "https://10.0.0.247:4000"
    t1, t2 = doc["teams"]
    assert t1["team_num"] == 1 and t2["team_num"] == 2
    assert [b["ip"] for b in t1["boxes"]] == ["192.168.125.2", "192.168.125.4",
                                             "192.168.125.5"]
    assert [b["ip"] for b in t2["boxes"]] == ["192.168.126.2", "192.168.126.4",
                                             "192.168.126.5"]
    ad = t1["boxes"][0]
    assert ad["os"] == "windows" and ad["port"] == 5985 and ad["connection"] == "winrm"
    assert ad["username"] == ".\\Administrator" and ad["password"] == "SECRETpw"
    assert ad["is_domain_controller"] is True
    assert ad["domain"] == "mira-125.corp.sus"
    assert ad["domain_username"] == "MIRA-125\\Administrator"
    fedora, ubuntu = t1["boxes"][1], t1["boxes"][2]
    assert fedora["os"] == "linux" and fedora["username"] == "blueteam"
    assert ubuntu["os"] == "linux"
    assert not any(b["os"] == "firewall" for t in doc["teams"] for b in t["boxes"])
    assert doc["scoring_account"]["username"] == "red_scoring"


def test_want_boxes(tmp_path):
    c = comp(tmp_path, [125])
    assert c3rberus_ops.want_boxes(c) == {"192.168.125.2", "192.168.125.4",
                                         "192.168.125.5"}


def test_render_env(tmp_path):
    c = comp(tmp_path, [125])
    env = c3rberus_ops.render_env(c, "BOXpw", red_ip="10.0.0.247", scoring_pw="SCORpw")
    kv = dict(l.split("=", 1) for l in env.strip().splitlines())
    assert kv["C2_IP"] == "10.0.0.247" and kv["SLIVER_ENABLED"] == "true"
    assert kv["SCRIPT_SERVER_URL"] == "https://10.0.0.247:4000"
    assert kv["RED_SCORING_PASSWORD"] == "SCORpw"
    assert kv["DEFAULT_WINDOWS_PASSWORD"] == "BOXpw"
    assert kv["SA_COLLECTION_ENABLED"] == "false"   # boxes pristine until T0
    assert kv["NMAP_HEALTH_ENABLED"] == "false"
    assert kv["DEV_MODE"] == "False"
    assert kv["MTLS_LISTENER_PORT"] == "38888"      # offset from tlaloc's 8888


def test_transport_map_extension_survives_the_firewall_normalizer():
    # run-c7f1dde4 died here: the first extension flattened the map to a port LIST and
    # the normalizer's dict.items() (plus a sorted() mixing names and ints) exploded.
    from red import red01_ops
    tlaloc_map = {"mtls": {"gateway": 8888, "server": "8888"}}
    specs = red01_ops._callback_ports(
        None, c3rberus_ops.extend_transport_map(tlaloc_map))
    assert [s["name"] for s in specs] == ["mtls", "c3rberus-mtls", "c3rberus-stage"]
    assert all(isinstance(s["server"], int) for s in specs)


def test_provision_unix_scoring_sudos_the_root_only_commands(monkeypatch):
    # the platform creator assumes root-password ssh; on goldens that refuse it its
    # unsudo'd chpasswd no-ops with rc 0 — the engine helper must carry sudo AND
    # force uid 0 on pre-existing accounts (run-1b76723f: "4 ok, 4 failed")
    import red.c3rberus_ops as m
    calls = []

    def fake_ssh(key, ip, remote, **kw):
        calls.append(remote)

        class R:
            stdout = "[+] blueteam:n0t_sus1  Linux - Shell access!"
            stderr = ""
        return R()
    monkeypatch.setattr(m, "_ssh", fake_ssh)
    inv = {"teams": [{"boxes": [
        {"ip": "192.168.127.2", "os": "windows", "username": ".\\Administrator"},
        {"ip": "192.168.127.4", "os": "linux", "username": "blueteam"}]}]}
    m._provision_unix_scoring("k", "10.0.0.247", inv, "n0t_sus1", "SCORpw")
    assert len(calls) == 1
    assert "sudo -n bash -c" in calls[0] and "usermod -o -u 0" in calls[0]
    assert "chpasswd" in calls[0] and "SCORpw" in calls[0]
