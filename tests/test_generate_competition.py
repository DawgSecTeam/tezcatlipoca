import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = Path(__file__).resolve().parents[1] / "generate-competition.py"
_mod = importlib.util.spec_from_file_location("generate_competition", _SPEC)
gc = importlib.util.module_from_spec(_mod)
_mod.loader.exec_module(gc)


def _spec(**over):
    spec = {"name": "mixed", "scenario": "mixed OS", "difficulty": 2,
            "boxes": [
                {"name": "web01", "os": "debian", "services": ["nginx", "ssh"]},
                {"name": "win01", "os": "windows",
                 "services": ["IIS FTP", {"name": "score/tcp", "score_only": True,
                                          "check": "Tcp", "display": "ftp", "port": 21}]},
            ]}
    spec.update(over)
    return spec


def test_spec_writes_a_mixed_os_competition(tmp_path):
    out = tmp_path / "mixed"
    gc.write(out, gc.build(_spec(compfile={"red.c2_engine": "sliver"})))
    assert json.loads((out / "boxes.json").read_text()) == [
        {"name": "web01", "last_octet": 2, "cpu": 1, "memory_mb": 2048,
         "disk_gb": None, "template": "base-debian13-lite-fix"},
        {"name": "win01", "last_octet": 3, "cpu": 1, "memory_mb": 2048,
         "disk_gb": None, "template": "base-windows-server"},
    ]
    assert json.loads((out / "box_services.json").read_text())["win01"][1]["port"] == 21
    assert "red.c2_engine sliver" in (out / "Compfile").read_text()
    assert json.loads((out / "users.json").read_text())["credlist_usernames"] == [
        "admin", "user1", "user2"]


def test_box_count_shorthand_expands(tmp_path):
    built = gc.build({"name": "counted",
                      "boxes": [{"count": 2, "os": "windows", "services": ["IIS FTP"]},
                                {"count": 2, "os": "debian", "services": ["ssh"]}]})
    assert [b["name"] for b in built["boxes"]] == ["box01", "box02", "box03", "box04"]
    assert [b["last_octet"] for b in built["boxes"]] == [2, 3, 4, 5]


def test_duplicate_octets_and_unknown_os_are_refused():
    with pytest.raises(gc.SpecError, match="duplicate last_octet"):
        gc.build({"name": "dup", "boxes": [
            {"os": "windows", "last_octet": 2, "services": []},
            {"os": "debian", "last_octet": 2, "services": []}]})
    with pytest.raises(gc.SpecError, match="set os"):
        gc.build({"name": "os", "boxes": [{"os": "plan9", "services": []}]})


def test_existing_directory_needs_force(tmp_path):
    out = tmp_path / "mixed"
    out.mkdir()
    (out / "keep").write_text("x")
    with pytest.raises(gc.SpecError, match="already exists"):
        gc.write(out, gc.build(_spec()))
    gc.write(out, gc.build(_spec()), force=True)
    assert (out / "Compfile").exists()
