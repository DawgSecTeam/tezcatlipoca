"""Comp-dir resolution: comps root fallbacks, --comp-dir precedence, path-or-name comps."""

from pathlib import Path

from common import comp_paths
from common.comp_paths import comps_root, resolve_comp_dir


def test_env_root_wins(monkeypatch, tmp_path):
    root = tmp_path / "from-env"
    root.mkdir()
    monkeypatch.setenv("TEZ_COMPETITIONS_ROOT", str(root))
    assert comps_root() == root


def test_cwd_competitions_wins_over_checkout(monkeypatch, tmp_path):
    (tmp_path / "competitions").mkdir()
    monkeypatch.delenv("TEZ_COMPETITIONS_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert comps_root() == Path("competitions")


def test_checkout_competitions_last_resort(monkeypatch, tmp_path):
    repo = tmp_path / "tezcatlipoca"
    repo.mkdir()
    monkeypatch.delenv("TEZ_COMPETITIONS_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)  # no competitions/ here, no sibling either
    monkeypatch.setattr(comp_paths, "REPO_ROOT", repo)
    assert comps_root() == repo / "competitions"


def test_sibling_repo_fallback(monkeypatch, tmp_path):
    repo = tmp_path / "tezcatlipoca"
    repo.mkdir()
    sibling = tmp_path / "tezcatlipoca-competitions"
    sibling.mkdir()
    monkeypatch.delenv("TEZ_COMPETITIONS_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(comp_paths, "REPO_ROOT", repo)
    assert comps_root() == sibling


def test_comp_dir_flag_wins_and_name_resolves_under_root(monkeypatch, tmp_path):
    monkeypatch.setenv("TEZ_COMPETITIONS_ROOT", str(tmp_path / "root"))
    assert resolve_comp_dir("comp-a", tmp_path / "workspace") == tmp_path / "workspace"
    assert resolve_comp_dir("comp-a") == tmp_path / "root" / "comp-a"
    assert resolve_comp_dir(comp_dir="just-a-path") == Path("just-a-path")


def test_cli_parser_accepts_comp_dir():
    from deploy_lib.cli import build_parser
    args = build_parser().parse_args(["--comp-dir", "/tmp/ws", "--teams", "2", "--yes"])
    assert args.comp_dir == "/tmp/ws" and args.teams == 2


def test_artifacts_resolve_comp_accepts_path_or_name(monkeypatch, tmp_path):
    from artifacts_lib.cli_common import resolve_comp
    named = tmp_path / "root" / "comp-a"
    named.mkdir(parents=True)
    (named / "Compfile").write_text("name comp-a\n")
    monkeypatch.setenv("TEZ_COMPETITIONS_ROOT", str(tmp_path / "root"))
    assert resolve_comp("comp-a") == named

    elsewhere = tmp_path / "run-workspace"
    elsewhere.mkdir()
    (elsewhere / "Compfile").write_text("name comp-a\n")
    assert resolve_comp(str(elsewhere)) == elsewhere
    assert resolve_comp("missing") is None
