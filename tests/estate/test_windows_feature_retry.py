"""A Windows feature install can hit ERROR_SHARING_VIOLATION (0x80070020) while setup or
Update holds the file; the bootstrap must retry that instead of failing the deploy
(live: run-f7976169 failed ad01's AD-Domain-Services install)."""

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from common import windows_ops


def test_the_feature_install_retries_a_sharing_violation():
    block = windows_ops._feature_block(["AD-Domain-Services"])
    assert "0x80070020" in block
    assert "3 attempts" in block
    assert "Install-WindowsFeature -Name @('AD-Domain-Services')" in block


def test_no_features_means_no_block():
    assert windows_ops._feature_block([]) == ""
