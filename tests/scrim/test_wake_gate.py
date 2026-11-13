import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from scrim import red_stage


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    started, waited = [], []
    life = types.ModuleType("infra.lifecycle")
    life.vm_status = lambda node, vmid: ("running" if vmid == 1470 else "stopped")
    life.start_vm = lambda node, vmid: started.append(vmid)
    ssh = types.ModuleType("common.ssh_ops")
    ssh.wait_for_boxes_ssh = lambda ctx, targets, timeout: waited.append(
        sorted(t["vmid"] for t in targets))
    pipe = types.ModuleType("pipeline_api")
    pipe.engine_ctx = lambda d: {"box_username": "ubuntu"}
    est = types.ModuleType("estate.state")
    est.load_flat = lambda comp: {"roster": {"targets": {
        "team1-ad01": {"vmid": 1470, "ip": "i2", "node": "n", "box_name": "ad01"},
        "team1-ftp01": {"vmid": 1471, "ip": "i3", "node": "n", "box_name": "ftp01"}}}}
    infra = types.ModuleType("infra"); infra.lifecycle = life
    common = types.ModuleType("common"); common.ssh_ops = ssh
    estate = types.ModuleType("estate"); estate.state = est
    for name, mod in [("infra", infra), ("infra.lifecycle", life),
                      ("common", common), ("common.ssh_ops", ssh),
                      ("pipeline_api", pipe), ("estate", estate), ("estate.state", est)]:
        monkeypatch.setitem(sys.modules, name, mod)
    (tmp_path / "boxes.json").write_text(json.dumps(
        [{"boxes": [{"name": "ad01", "template": "base-linux"},
                    {"name": "ftp01", "template": "base-windows-server"}]}]))
    return SimpleNamespace(started=started, waited=waited, comp=str(tmp_path))


def test_a_cold_guest_gets_woken_and_every_guest_gets_probed(fakes):
    n = red_stage.ensure_range_awake(fakes.comp, {})
    assert fakes.started == [1471]                      # only the stopped one boots
    assert fakes.waited == [[1470, 1471]]               # readiness is proven for all
    assert n == 2


def test_a_fully_awake_range_powers_on_nothing(fakes, monkeypatch):
    life = sys.modules["infra.lifecycle"]
    monkeypatch.setattr(life, "vm_status", lambda node, vmid: "running")
    red_stage.ensure_range_awake(fakes.comp, {})
    assert fakes.started == []
