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
    # dotenv reads the LAST value of a duplicate key, so the kv dict above hides
    # dups — run-8e22's convoy proved a silent NXC_MAX_CONCURRENT=140-shadowed-
    # by-20 can ship green. Assert the RAW file: every key exactly once, and the
    # convoy math (pool = limit+20 threads; per-box must exceed putfile 1 + exec).
    keys = [l.split("=", 1)[0] for l in env.strip().splitlines() if "=" in l]
    assert len(keys) == len(set(keys)), [k for k in set(keys) if keys.count(k) > 1]
    assert kv["NXC_MAX_CONCURRENT"] == "140"
    assert 12 >= int(kv["NXC_MAX_CONCURRENT_PER_BOX"]) >= 8  # > putfile+exec, < MaxStartups


def test_transport_map_extension_survives_the_firewall_normalizer():
    # run-c7f1dde4 died here: the first extension flattened the map to a port LIST and
    # the normalizer's dict.items() (plus a sorted() mixing names and ints) exploded.
    from red import red01_ops
    tlaloc_map = {"mtls": {"gateway": 8888, "server": "8888"}}
    specs = red01_ops._callback_ports(
        None, c3rberus_ops.extend_transport_map(tlaloc_map))
    assert [s["name"] for s in specs] == ["mtls", "c3rberus-mtls", "c3rberus-stage"]
    assert all(isinstance(s["server"], int) for s in specs)


def test_render_inventory_never_pins_scoring_password(tmp_path):
    # .env's seeded password is the ONE scoring password: the creator provisions
    # AD+local SAM accounts with it and _provision_linux_scoring sets uid-0 Linux
    # to it. A per-box scoring_password outranks .env in the platform's resolver
    # and strands the domain channel — MIRA\red_scoring LOGON_FAILURE rotation on
    # members (run-fca282ee): the packet decoy pw must stay in box_baseline.json.
    import yaml
    c = comp(tmp_path, [125])
    (c / "box_baseline.json").write_text(json.dumps({
        "web01": [{"name": "local-user", "vars": {"USERNAME": "red_scoring",
                                                  "PASSWORD": "pktW3b"}}],
        "ad01": [{"name": "local-user-win", "vars": {"USERNAME": "red_scoring",
                                                     "PASSWORD": "pktWad"}}]}))
    doc = yaml.safe_load(c3rberus_ops.render_inventory(c, "SECRETpw", red_ip="10.0.0.247"))
    assert not any("scoring_password" in b for team in doc["teams"] for b in team["boxes"])
    assert doc["scoring_account"]["username"] == "red_scoring"
    doc = yaml.safe_load(c3rberus_ops.render_inventory(
        c, "SECRETpw", red_ip="10.0.0.247", fetch_source="10.0.0.251"))
    assert all(b.get("fetch_source_ip") == "10.0.0.251"
               for team in doc["teams"] for b in team["boxes"])


def test_provision_linux_scoring_lands_root_and_rejects_failure(monkeypatch):
    import red.c3rberus_ops as m
    calls = []

    def fake_ssh(key, ip, remote, **kw):
        calls.append(remote)

        class R:
            stdout = "\x1b[0mSSH  ip 22 ip    TEZPROVOK\x1b[0m\r\n"
            stderr = ""
        return R()
    monkeypatch.setattr(m, "_ssh", fake_ssh)
    inv = {"teams": [{"boxes": [
        {"ip": "192.168.127.2", "os": "windows", "username": ".\\Administrator"},
        {"ip": "192.168.127.4", "os": "linux", "username": "blueteam"}]}]}
    m._provision_linux_scoring("k", "10.0.0.247", inv, "n0t_sus1", "SCORpw")
    assert len(calls) == 1
    assert "red_scoring:x:0:0::/root:/bin/bash" in calls[0]
    assert "PermitRootLogin yes" in calls[0] and "SCORpw" in calls[0]
    assert "| tail" not in calls[0]                    # judge nxc's rc, not a pipe

    def bad_ssh(key, ip, remote, **kw):
        class R:
            stdout = "usermod: used by process 1"  # no sentinel
            stderr = ""
        return R()
    monkeypatch.setattr(m, "_ssh", bad_ssh)
    import pytest
    with pytest.raises(RuntimeError):
        m._provision_linux_scoring("k", "10.0.0.247", inv, "n0t_sus1", "SCORpw")


def test_probe_fetch_source_extracts_tagged_client(monkeypatch):
    import red.c3rberus_ops as m
    seen = {}

    def fake_ssh(key, ip, remote, **kw):
        seen["remote"] = remote

        class R:
            stdout = "10.0.0.251\n"
            stderr = ""
        return R()
    monkeypatch.setattr(m, "_ssh", fake_ssh)
    import pathlib
    comp_dir = pathlib.Path(__file__).parent / "fixtures" / "comp_min"
    if not (comp_dir / "teams.json").exists():
        monkeypatch.setattr(m, "_teams", lambda d: {"t1": {"identifier": 127}})
        monkeypatch.setattr(m, "_boxes", lambda d: [{"name": "web01", "last_octet": 4,
                                                     "template": "ubuntu-golden"}])
        monkeypatch.setattr(m, "box_os", lambda t: "linux")
    nat = m._probe_fetch_source("k", "10.0.0.247", comp_dir, "pw")
    assert nat == "10.0.0.251"
    assert "curl -sk" in seen["remote"] and "192.168.127.4" in seen["remote"]
    assert "Authorization: Bearer" in seen["remote"]          # bound to THIS probe, not history


def test_stage_rewrite_targets_the_env_declared_inventory(monkeypatch):
    # the backend loads INVENTORY_FILE=config/inventory.yml — rewriting the
    # top-level file (where bootstrap STAGED it) is a silent no-op (run-9fcc1b2c)
    import red.c3rberus_ops as m
    calls = []

    def fake_ssh(key, ip, remote, **kw):
        calls.append((kw.get("input"), remote))

        class R:
            returncode, stdout, stderr = 0, "10.0.0.251\n", ""
        return R()
    monkeypatch.setattr(m, "_ssh", fake_ssh)
    monkeypatch.setattr(m, "_probe_fetch_source", lambda *a, **k: "10.0.0.251")
    monkeypatch.setattr(m, "wait_backend", lambda *a, **k: None)
    monkeypatch.setattr(m, "_api", lambda *a, **k: {"status": "ok", "boxes": []})
    monkeypatch.setattr(m, "cred_preflight", lambda *a, **k: None)
    monkeypatch.setattr(m, "_provision_linux_scoring", lambda *a, **k: None)
    monkeypatch.setattr(m, "plant", lambda *a, **k: [])
    import pathlib
    root = pathlib.Path(c3rberus_ops.__file__).parent  # unused; keep import light
    src = pathlib.Path(m.__file__).read_text()
    assert "tee /opt/c3rberus/config/inventory.yml" in src


def test_plant_counts_dict_rows_and_tolerates_stragglers(tmp_path, monkeypatch, capsys):
    # run-24737a0d: the OLD code read 0 rows (dict wrapper) and raised on the
    # parent's per-IOC 'failed' status while 1107 rows on 8 healthy boxes sat
    # planted. plant must count real rows and tolerate stragglers ONLY when
    # every box carries a catalog.
    import json as _json
    (tmp_path / "boxes.json").write_text(_json.dumps(
        {f"b{i}": {"name": f"b{i}"} for i in range(4)}))
    m = c3rberus_ops
    monkeypatch.setattr(m, "_wait_task", lambda *a, **k: "failed")
    boxes = [f"192.168.12{t}.{n}" for t in (7, 8) for n in range(2, 6)]
    rows = [{"box_ip": ip, "status": "active"} for ip in boxes for _ in range(60)]
    def fake_api(ssh_key, red_ip, token, method, path, **kw):
        return {"task_id": 7} if method == "POST" else {"deployed": rows}
    monkeypatch.setattr(m, "_api", fake_api)
    got = m.plant("k", "10.0.0.247", "t", [104, 105], comp_dir=str(tmp_path))
    assert got == len(rows)                      # was 0 under the len(list) bug
    assert "WARNING tolerating" in capsys.readouterr().out

    # a box stripped of its catalog is NOT tolerable
    rows.append({"box_ip": "192.168.129.9", "status": "active"})  # noise
    thin = [r for r in rows if r["box_ip"] != boxes[0]]
    rows2 = [r for r in rows if not (r["box_ip"] == boxes[0] and rows.index(r) > 10)]
    def api2(ssh_key, red_ip, token, method, path, **kw):
        if method == "POST":
            return {"task_id": 7}
        return {"deployed": [r for r in rows if r["box_ip"] != boxes[0]] +
                             [r for r in rows if r["box_ip"] == boxes[0]][:5]}
    monkeypatch.setattr(m, "_api", api2)
    try:
        m.plant("k", "10.0.0.247", "t", [104, 105], comp_dir=str(tmp_path))
        assert False, "must raise on a catalog-less box"
    except RuntimeError as e:
        assert "catalog-carrying boxes" in str(e)


def _bootstrap_fakes(monkeypatch, m, accept):
    """Stateful fake DB: `accept` is the live password set; change_password
    mutates it exactly like a rotation does — nothing the code believes can
    log in is allowed to be a fiction."""
    log = {"rm": 0, "rotated": []}
    live = set(accept)
    monkeypatch.setattr(m, "wait_backend", lambda *a, **k: None)
    def _login_or_none(key, ip, pw, **k):
        return ("tok-" + pw[:4], 1) if pw in live else None
    def _login(key, ip, pw, **k):
        if pw not in live:
            raise RuntimeError(f"c3rberus login refused: {pw}")
        return ("tok-" + pw[:4], 1)
    def _api(key, ip, token, method, path, **k):
        if "change_password" in path:
            pw = k["body"]["password"]
            log["rotated"].append(pw)
            live.add(pw)
        return {}
    def _ssh(key, ip, remote, **k):
        import subprocess
        if "database.db" in remote:
            log["rm"] += 1
            live.update(accept if "fresh" not in remote else set())
        return subprocess.CompletedProcess([], 0, "", "")
    for name, fn in [("_login_or_none", _login_or_none), ("_login", _login),
                     ("_api", _api), ("_ssh", _ssh)]:
        monkeypatch.setattr(m, name, fn)
    return log


def test_stale_seed_readopts_the_rebuilt_platform(monkeypatch, tmp_path):
    # run-327c994e: destroy-and-rebuild = seed doc old, DB factory-fresh. The
    # seeded password must lose to reality and be re-rotated BACK so every
    # already-published credential stays true.
    from red import c3rberus_ops as m
    seeded = tmp_path / "red" / "c3rberus-seed.json"
    seeded.parent.mkdir(); seeded.write_text('{"admin_password": "seeded-pw"}')
    log = _bootstrap_fakes(monkeypatch, m, {"seeded-pw"})
    tok = m._bootstrap_admin("k", "ip", {"admin_password": "seeded-pw"},
                             "seeded-pw", "sc", seeded)
    assert tok == "tok-seed" and log["rm"] == 0 and log["rotated"] == []

    monkeypatch.undo()
    log2 = _bootstrap_fakes(monkeypatch, m, {m.DEFAULT_ADMIN[1]})
    tok2 = m._bootstrap_admin("k", "ip", {"admin_password": "seeded-pw"},
                              "seeded-pw", "sc", seeded)
    assert tok2 == "tok-seed" and log2["rm"] == 0          # default path, no wipe
    assert log2["rotated"] == ["seeded-pw"]              # rotated BACK to seed

    monkeypatch.undo()
    log3 = {"rm": 0, "rotated": []}                    # DB answers to nobody yet
    monkeypatch.setattr(m, "wait_backend", lambda *a, **k: None)
    def _none3(key, ip, pw, **k):
        return ("tok-phon", 1) if (pw == m.DEFAULT_ADMIN[1] and log3["rm"]) else None
    def _hard3(key, ip, pw, **k):
        if log3["rm"] and pw in (m.DEFAULT_ADMIN[1], "seeded-pw"):
            return ("tok-seed" if pw == "seeded-pw" else "tok-phon", 1)
        raise RuntimeError("c3rberus login refused")
    def _api3(key, ip, token, method, path, **k):
        log3["rotated"].append(k.get("body", {}).get("password", path))
        return {}
    def _ssh3(key, ip, remote, **k):
        import subprocess
        if "database.db" in remote:
            log3["rm"] += 1
        return subprocess.CompletedProcess([], 0, "", "")
    for n3, f3 in [("_login_or_none", _none3), ("_login", _hard3),
                   ("_api", _api3), ("_ssh", _ssh3)]:
        monkeypatch.setattr(m, n3, f3)
    tok3 = m._bootstrap_admin("k", "ip", {"admin_password": "seeded-pw"},
                              "seeded-pw", "sc", seeded)
    assert log3["rm"] == 1 and log3["rotated"] == ["seeded-pw"] and tok3 == "tok-seed"


def test_fetch_source_probe_retries_past_server_warmup(monkeypatch, tmp_path):
    import subprocess
    from red import c3rberus_ops as m
    calls = {"n": 0}
    boxes = [{"template": "linux", "last_octet": 4, "in_path": False}]
    monkeypatch.setattr(m, "_boxes", lambda cd: boxes)
    monkeypatch.setattr(m, "_teams", lambda cd: {1: {"identifier": 127}})
    monkeypatch.setattr(m, "box_os", lambda t: "linux")
    monkeypatch.setattr(m, "time", type("T", (), {"sleep": staticmethod(lambda s: None),
                                                  "time": staticmethod(lambda: 1700000000)}))
    def ssh(key, ip, remote, **k):
        calls["n"] += 1
        out = "10.0.0.251" if calls["n"] >= 3 else ""
        return subprocess.CompletedProcess([], 0, out, "")
    monkeypatch.setattr(m, "_ssh", ssh)
    assert m._probe_fetch_source("k", "10.0.0.247", str(tmp_path), "pw") == "10.0.0.251"
    assert calls["n"] == 3
    calls["n"] = 0
    monkeypatch.setattr(m, "_ssh", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
    try:
        m._probe_fetch_source("k", "10.0.0.247", str(tmp_path), "pw")
        assert False, "must raise after exhausting retries"
    except RuntimeError as e:
        assert "after 6 attempts" in str(e)


def test_wait_backend_gates_the_script_server_too(monkeypatch):
    from red import c3rberus_ops as m
    calls = {"n": 0}
    monkeypatch.setattr(m, "time", type("T", (), {
        "sleep": staticmethod(lambda s: None),
        "time": staticmethod(lambda: 1700000000.0)}))
    def ssh(key, ip, remote, **k):
        import subprocess
        calls["n"] += 1
        if "/docs" in remote:
            return subprocess.CompletedProcess([], 0, "200", "")
        # :4000 silent on the first two rounds (cold), answers 401 after
        return subprocess.CompletedProcess([], 0, "" if calls["n"] < 5 else "401", "")
    monkeypatch.setattr(m, "_ssh", ssh)
    m.wait_backend("k", "10.0.0.247")          # must NOT return before :4000 speaks
    assert calls["n"] >= 5
    monkeypatch.setattr(m, "_ssh", lambda *a, **k: __import__("subprocess")
                        .CompletedProcess([], 0, "000", "") if "/scripts" in a[2] or ":4000" in a[2]
                        else __import__("subprocess").CompletedProcess([], 0, "200", ""))
    # time now always advances past deadline: loop must terminate by timeout
    tt = {"t": 0.0}
    def clock():
        tt["t"] += 16
        return 1700000000.0 + tt["t"]
    monkeypatch.setattr(m, "time", type("T", (), {"sleep": staticmethod(lambda s: None),
                                                  "time": staticmethod(clock)}))
    try:
        m.wait_backend("k", "10.0.0.247", timeout_s=120)
        assert False, "must raise when :4000 never speaks"
    except RuntimeError as e:
        assert "scripts=" in str(e)
