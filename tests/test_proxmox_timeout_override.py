"""proxmox_api_for: a caller's own timeout must not collide with the wrapper's default."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from infra import client  # noqa: E402


class _Response:
    def raise_for_status(self):
        pass

    def json(self):
        return {"data": []}


class TimeoutOverride(unittest.TestCase):
    def _seen_timeout(self, **kwargs):
        seen = {}

        def fake_request(method, url, **kw):
            seen.update(kw)
            return _Response()

        with patch.object(client, "proxmox_request", fake_request):
            client.proxmox_api_for("https://pve:8006", "tok", "GET", "/cluster/resources",
                                   **kwargs)
        return seen["timeout"]

    def test_a_callers_timeout_is_honored(self):
        # list_proxmox_templates passes timeout=10; the wrapper's own timeout=60 raised
        # "got multiple values for keyword argument 'timeout'", which that caller swallowed
        # into an empty template list (the picker read "0 of 17 live", 2026-10-15).
        self.assertEqual(self._seen_timeout(params={"type": "vm"}, timeout=10), 10)

    def test_the_wrapper_default_still_applies(self):
        self.assertEqual(self._seen_timeout(), 60)
