"""The seed runs twice (deploy phase 7, then the scrim's stage_red): a persistence family
already standing is the seed's goal met, not a failed action (live: cde-2026 counted 8)."""

from tlaloc import seed
from tlaloc.effects.persist import auto_order


class _World:
    def __init__(self, arts):
        self._arts = arts

    def artifacts(self, ip):
        return self._arts

    def foothold(self, ip):
        return {"ip": ip}


class _Ctx:
    def __init__(self, arts):
        self.world = _World(arts)
        self.c2 = None

    def is_windows(self, ip):
        return False


def test_a_standing_family_is_the_seeds_goal_met(monkeypatch):
    monkeypatch.setattr(seed, "execute", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("seed re-planted a standing family")))
    arts = [{"family": f, "kind": "artifact"} for f in auto_order(False)]
    out = seed._run_stage(_Ctx(arts), "192.168.105.4", "persist", "full")
    assert out["ok"] is True
    assert "already standing" in out["detail"]


def test_evasion_is_idempotent_too(monkeypatch):
    monkeypatch.setattr(seed, "execute", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("seed re-planted a standing evasion family")))
    ctx = _Ctx([{"family": "immu", "kind": "artifact"}])
    out = seed._run_evasion(ctx, "192.168.105.4")
    assert out["ok"] is True or "already standing" in out["detail"]
