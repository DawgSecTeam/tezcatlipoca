"""A template-derived engine never had team NAT: the template lacks the range-firewall units
(checked live on a built template) and kernel iptables never survives a clone, so the golden
builds on the team bridges could not reach the archive (live: run-62f9e120). The template path
must install the same firewall/healthcheck the build path does."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from engine import engine_identity_ops


def test_prepare_from_template_installs_the_range_firewall():
    with mock.patch.object(engine_identity_ops, "_run_engine_cmd"), \
         mock.patch.object(engine_identity_ops, "push_quotient_env"), \
         mock.patch.object(engine_identity_ops, "_install_firewall_and_healthcheck") as fw:
        engine_identity_ops.prepare_engine_from_template(SimpleNamespace(), "pg", "redis")
    fw.assert_called_once()
