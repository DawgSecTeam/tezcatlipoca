"""A placement whose uplink filters a public resolver names its own resolver first
(TEZ_BOX_DNS); the default pair keeps the previous behavior."""

import importlib

from common import utils


def test_tez_box_dns_names_the_placement_resolver_first(monkeypatch):
    monkeypatch.setenv("TEZ_BOX_DNS", "10.0.0.1 1.1.1.1")
    mod = importlib.reload(utils)
    try:
        assert mod.BOX_DNS_PRIMARY == "10.0.0.1"
        assert mod.BOX_DNS == "10.0.0.1 1.1.1.1"
        assert "nameserver 10.0.0.1" in mod.DNS_FIX_CMD
    finally:
        monkeypatch.delenv("TEZ_BOX_DNS")
        importlib.reload(utils)


def test_the_default_is_the_public_pair():
    assert utils.BOX_DNS == "1.1.1.1 8.8.8.8"
    assert utils.BOX_DNS_PRIMARY == "1.1.1.1"
