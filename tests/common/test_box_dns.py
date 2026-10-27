"""A placement whose uplink filters a public resolver names its own resolver first
(TEZ_BOX_DNS); the default pair keeps the previous behavior.

The value is resolved per call, not at import: the deploy entrypoints load `.env` after
`common.utils` is first imported.
"""

import pytest

from common import utils


def test_tez_box_dns_names_the_placement_resolver_first(monkeypatch):
    monkeypatch.setenv("TEZ_BOX_DNS", "10.0.0.1 1.1.1.1")
    assert utils.box_dns_primary() == "10.0.0.1"
    assert utils.box_dns() == "10.0.0.1 1.1.1.1"
    assert "nameserver 10.0.0.1" in utils.dns_fix_cmd()


def test_the_default_is_the_public_pair():
    assert utils.box_dns() == "1.1.1.1 8.8.8.8"
    assert utils.box_dns_primary() == "1.1.1.1"


def test_a_non_address_entry_is_refused_by_name(monkeypatch):
    monkeypatch.setenv("TEZ_BOX_DNS", "8.8.8.8; reboot")
    with pytest.raises(SystemExit) as caught:
        utils.box_dns_servers()
    assert "box_dns" in str(caught.value)
