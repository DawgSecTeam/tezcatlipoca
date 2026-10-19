"""portal-access.py resync: re-apply the recorded token's ACLs after a box rebuild."""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("portal_access_cli_under_test",
                                               ROOT / "portal-access.py")
portal_access = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(portal_access)

TEAMS = {"team1": {"identifier": "101", "password": "pw"}}
NODES = {"pve": {"pve_node": "proxmox", "endpoint": "https://203.0.113.10:8006",
                 "token_env": "TOK", "tls_fingerprint": "", "vmids": [1210]}}
USER = "tezcon-probe-run-abc@pve"
PREVIOUS = {"pve": {"user": USER, "token_id": f"{USER}!portal", "secret": "s3"}}


class Resync(unittest.TestCase):
    def test_reapplies_the_recorded_token_without_recreating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            (comp_dir / "teams.json").write_text(json.dumps(TEAMS))
            (comp_dir / "boxes.json").write_text("[]")
            state = {"run_id": "run-abc", "portal_console_tokens": PREVIOUS}
            seen, saved = {}, {}

            def fake_mint(comp_name, run_id, nodes, previous=None):
                seen.update(comp=comp_name, run_id=run_id, nodes=nodes, previous=previous)
                return {"pve": dict(previous["pve"])}, []

            with mock.patch.object(portal_access.estate_state, "load_flat",
                                   return_value=state), \
                    mock.patch.object(portal_access.estate_state, "write_flat",
                                      side_effect=lambda d, s: saved.update(s)), \
                    mock.patch.object(portal_access, "read_placement", return_value=None), \
                    mock.patch.object(portal_access, "load_targets", return_value=[]), \
                    mock.patch.object(portal_access.config, "active",
                                      return_value=SimpleNamespace(
                                          proxmox=SimpleNamespace(node="pve"))), \
                    mock.patch.object(portal_access, "node_targets", return_value=NODES), \
                    mock.patch.object(portal_access, "mint_console_tokens",
                                      side_effect=fake_mint), \
                    contextlib.redirect_stdout(io.StringIO()):
                portal_access.resync(comp_dir)
        self.assertIs(seen["previous"], PREVIOUS)
        self.assertEqual(seen["run_id"], "run-abc")
        self.assertEqual(seen["nodes"], NODES)
        self.assertEqual(saved["portal_console_tokens"], PREVIOUS)


if __name__ == "__main__":
    unittest.main()
