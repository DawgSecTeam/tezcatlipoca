"""red01's apt prelude runs right after the clone boots, when cloud-init (and
unattended-upgrades) may still hold apt's lists lock. `DPkg::Lock::Timeout` covers the dpkg
lock, not the lists lock `update` takes, so the prelude waits for cloud-init and retries the
update before it fails a whole deploy (live: run-00488d45's cde deploy died at its last step)."""

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from red import red01_ops


def test_the_prelude_waits_for_cloud_init_and_retries_the_update():
    script = red01_ops._apt_prelude()
    assert "cloud-init status --wait" in script
    assert "for i in $(seq 1 12)" in script
    assert "apt-get -o DPkg::Lock::Timeout=600 update" in script
    assert red01_ops.DEPS in script
