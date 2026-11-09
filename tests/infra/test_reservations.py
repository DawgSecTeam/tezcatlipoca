import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from infra import reservations


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "slots.json"
    monkeypatch.setenv("TEZ_SLOT_LEDGER", str(path))
    return path


def test_claim_then_second_owner_conflicts(ledger, tmp_path):
    first = tmp_path / "comp-a"
    second = tmp_path / "comp-b"
    assert reservations.claim(first, [2000, 2001], run_id="run-a") == []
    conflicts = reservations.claim(second, [2001, 2002], run_id="run-b")
    assert [resource for resource, _ in conflicts] == ["2001"]
    assert conflicts[0][1]["run_id"] == "run-a"


def test_same_workspace_reclaims_its_own_slots(ledger, tmp_path):
    # A resume is the same competition and must not collide with itself.
    comp = tmp_path / "comp"
    assert reservations.claim(comp, [2000], run_id="run-1") == []
    assert reservations.claim(comp, [2000], run_id="run-2") == []
    assert json.loads(ledger.read_text())["2000"]["run_id"] == "run-2"


def test_release_frees_the_slots(ledger, tmp_path):
    comp = tmp_path / "comp"
    reservations.claim(comp, [2000, "vmbr180"])
    reservations.release(comp)
    assert ledger.exists()
    assert json.loads(ledger.read_text()) == {}
    assert reservations.claim(tmp_path / "other", [2000]) == []


def test_expired_reservation_does_not_block(ledger, tmp_path):
    reservations.claim(tmp_path / "comp", [2000], ttl=-1)
    assert reservations.claim(tmp_path / "other", [2000]) == []


def test_release_is_scoped_to_the_owner(ledger, tmp_path):
    mine, theirs = tmp_path / "a", tmp_path / "b"
    reservations.claim(mine, [2000])
    reservations.claim(theirs, [2001])
    reservations.release(mine)
    assert sorted(json.loads(ledger.read_text())) == ["2001"]


def test_preflight_refusal_names_the_holder(ledger, tmp_path, monkeypatch):
    """The refusal must keep the "vmid collisions: [...]" shape AND say who holds the slot."""
    from infra import gates
    import infra.collisions as collisions

    monkeypatch.setattr(collisions, "expected_slots",
                        lambda share, plan: [SimpleNamespace(vmid=2000, label="team vmid 2000")])
    monkeypatch.setattr(collisions, "expected_bridges", lambda share, boxes: ["vmbr180"])
    reservations.claim(tmp_path / "other-comp", [2000], run_id="run-other")

    with pytest.raises(SystemExit) as exc:
        gates.reserve_planned_slots(SimpleNamespace(run_id="run-mine"), tmp_path / "mine",
                                    SimpleNamespace(shares=[object()], boxes=[object()]))
    message = str(exc.value)
    assert "vmid collisions: [2000]" in message
    assert "run-other" in message
