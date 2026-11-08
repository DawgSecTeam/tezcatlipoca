"""A capped cycle must reap opencode's managed server BEFORE draining its pipes.

The `serve --service` grandchild has its own session, so killpg misses it; it also
holds the cycle's stdout. Draining first blocked until that grandchild's idle timer
(~16 min per cycle observed live), so blue completed a handful of cycles in a
2-hour event instead of one per ~6 min. Offline: a fake tree, order asserted."""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))


class ReapBeforeDrain(unittest.TestCase):

    def test_the_managed_server_is_reaped_before_pipes_are_drained(self):
        src = (_REPO / "scrim" / "blue_agent.py").read_text()
        reap = src.index('_reap_managed_servers(child_env.get("HOME", ""))')
        drain = src.index("out, err = proc.communicate()", reap - 1)
        self.assertLess(reap, drain, "the reap must precede the drain")
