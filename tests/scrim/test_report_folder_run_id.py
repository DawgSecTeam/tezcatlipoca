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
