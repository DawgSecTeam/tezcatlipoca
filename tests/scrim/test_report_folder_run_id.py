"""A fresh scrim mints its report folder before the deploy has a run id; once the deploy
records one the folder must take that name (live: run-9128c13a reported under untagged-*)."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from scrim import test_folder


def _run(tmp_path, run_id):
    comp = tmp_path / "comp"
    comp.mkdir()
    (comp / "Compfile").write_text("name demo\n")
    if run_id:
        (comp / "estate-state.json").write_text(json.dumps(
            {"identity": {"run_id": run_id}}))
    folder = comp / ".automated-tests" / "untagged-20261020-091614"
    folder.mkdir(parents=True)
    (folder / "test.json").write_text(json.dumps(
        {"schema": 1, "key": folder.name, "run_id": None,
         "paths": {"run_dir": str(folder)}}))
    return comp, folder


def test_the_folder_takes_the_runs_name(tmp_path):
    comp, folder = _run(tmp_path, "run-9128c13a")
    # a scrim's run_dir and test_dir are the same path
    args = SimpleNamespace(test_dir=str(folder), run_dir=str(folder))
    target = test_folder.adopt_run_key(args, comp)
    assert target == folder.parent / "run-9128c13a"
    assert target.is_dir() and not folder.exists()
    manifest = json.loads((target / "test.json").read_text())
    assert manifest["key"] == "run-9128c13a" and manifest["run_id"] == "run-9128c13a"
    assert manifest["paths"]["run_dir"] == str(target)
    assert args.test_dir == str(target)
    assert args.run_dir == str(target), "a stale run_dir breaks the next manifest write"


def test_without_a_run_id_it_stays_untagged(tmp_path):
    comp, folder = _run(tmp_path, None)
    args = SimpleNamespace(test_dir=str(folder))
    assert test_folder.adopt_run_key(args, comp) is None
    assert folder.is_dir()


def test_the_cli_follows_the_folder_the_deploy_renamed(tmp_path, monkeypatch):
    """The rename happens inside stage_deploy; cli.main's LOCAL run_dir must follow it or
    the first post-deploy manifest write lands in the renamed-away directory (live twice)."""
    import sys
    from scrim import cli, staging, fire_test, blue_agent, red_link, red_stage, inject_sync, event_run

    comp = tmp_path / "comp"
    comp.mkdir()
    (comp / "Compfile").write_text("name c1\n")
    (comp / "teams.json").write_text(json.dumps({"team1": {"identifier": "105", "password": "x"}}))
    (comp / "users.json").write_text(json.dumps({"box_username": "medic"}))
    (comp / "packet.md").write_text("packet\n")

    def fake_deploy(args, comp):
        (comp / "estate-state.json").write_text(json.dumps(
            {"identity": {"run_id": "run-abc12345"}}))
        test_folder.adopt_run_key(args, comp)

    monkeypatch.setattr(staging, "stage_deploy", fake_deploy)
    monkeypatch.setattr(staging, "creds_from_files",
                        lambda c: {"ENGINE_IP": "x", "TEAM1_ID": "105", "KEY_PATH": "k",
                                   "VM_USER": "u"})
    monkeypatch.setattr(staging, "warn_co_tenancy", lambda *a: None)
    monkeypatch.setattr(fire_test, "stage_verify", lambda *a: None)
    monkeypatch.setattr(blue_agent, "stage_blues", lambda *a: None)
    monkeypatch.setattr(red_stage, "stage_red", lambda *a: None)
    monkeypatch.setattr(red_link, "start_red_director", lambda *a: None)
    monkeypatch.setattr(inject_sync, "reanchor_injects", lambda *a: None)
    monkeypatch.setattr(event_run, "run_event_and_finish", lambda *a: None)
    monkeypatch.setattr(sys, "argv", ["run-agent-scrim.py", "--competition", "c1",
                                      "--comp-dir", str(comp), "--teams", "1",
                                      "--duration-min", "1"])
    cli.main()

    folders = [p for p in (comp / ".automated-tests").iterdir() if p.is_dir()]
    assert [p.name for p in folders] == ["run-abc12345"], folders
    assert (folders[0] / "T0.txt").is_file()
    assert (folders[0] / "run.json").is_file()
