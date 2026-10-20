"""box_password is a golden-hash input, and the workspace state is per-run: a fresh run of a
competition that mints a new one rebuilds every golden the kept templates exist to avoid
(live: scrim-one, six goldens rebuilt on every fresh run)."""

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from deploy_lib import secrets as secrets_mod


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_mod, "COMP_SECRETS_DIR", tmp_path / "comp-secrets")


def test_a_fresh_run_of_the_same_competition_carries_the_same_password(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    first = secrets_mod.carry_box_password(None, "scrim-one")
    assert secrets_mod.carry_box_password(None, "scrim-one") == first
    assert (tmp_path / "comp-secrets" / "scrim-one.json").stat().st_mode & 0o777 == 0o600


def test_another_competition_gets_its_own_password(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    assert (secrets_mod.carry_box_password(None, "scrim-one")
            != secrets_mod.carry_box_password(None, "cde-2026"))


def test_the_workspaces_own_state_still_wins(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    stored = secrets_mod.carry_box_password(None, "scrim-one")
    assert secrets_mod.carry_box_password({"box_password": "carried"}, "scrim-one") \
        == "carried"
    assert stored != "carried"
