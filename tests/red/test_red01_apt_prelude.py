"""The red01 apt prelude must disarm unattended-upgrades before touching apt.

run-f199029d (live): a fresh red01 clone's unattended-upgrades held
/var/lib/dpkg/lock-frontend for >600s; DPkg::Lock::Timeout expired and phase 7
died rc=100 twice. The golden-side prep survived the same storm because it
MASKS and KILLS first — the prelude now does the same. (B2, 2026-11-09.)
"""

from red.red01_ops import _apt_prelude


def test_prelude_disarms_unattended_before_apt():
    body = _apt_prelude()
    apt_at = body.index("apt-get")
    assert "mask --now" in body and body.index("mask --now") < apt_at
    # exact-name kills only: -f would match this very script's -c line and self-kill
    assert "pkill -9 -x unattended-upgrade" in body
    assert "pkill -9 -f" not in body
    assert "dpkg --configure -a" in body
    assert "lock-frontend" in body                       # stale locks dropped
    assert "DPkg::Lock::Timeout" in body                 # belt stays behind the mechanism


def test_prelude_still_installs_deps():
    body = _apt_prelude()
    from red.red01_ops import DEPS
    assert "install -y -qq " + DEPS in body
