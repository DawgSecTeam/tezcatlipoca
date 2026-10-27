"""The template-reuse path starts the engine's compose stack on every run, and a fresh
volume's Postgres can miss compose's health window so the server exits 2 once. One failed
`compose up` is a race, not a dead engine (live: run-e5fe9b87's 2-team deploy failed here;
the same command succeeded a minute later)."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from engine import engine_cmd_ops, engine_identity_ops


def _run(failures):
    attempts = []

    def fake(ctx, cmd, **kw):
        if kw.get("step") == "compose up":
            attempts.append(1)
            if len(attempts) <= failures:
                raise RuntimeError("compose up failed")
        return mock.Mock()

    # The retry lives in engine_cmd_ops.start_quotient now; the grow-root/dns steps still go
    # through this module's binding. Both must see the fake.
    with mock.patch.object(engine_cmd_ops, "_run_engine_cmd", side_effect=fake), \
         mock.patch.object(engine_identity_ops, "_run_engine_cmd", side_effect=fake), \
         mock.patch.object(engine_identity_ops, "push_quotient_env"), \
         mock.patch.object(engine_identity_ops, "_install_firewall_and_healthcheck"), \
         mock.patch.object(engine_identity_ops.time, "sleep"):
        engine_identity_ops.prepare_engine_from_template(SimpleNamespace(), "pg", "redis")
    return attempts


def test_a_transient_compose_failure_is_retried():
    assert len(_run(1)) == 2


def test_three_failures_still_fail_the_deploy():
    with pytest.raises(RuntimeError, match="compose up failed"):
        _run(3)
