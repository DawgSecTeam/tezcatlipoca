"""Every webui/server.py route, offline: temp competitions/ root, fixture catalog, Popen
replaced by a stub. The web UI was not part of the 0.2.0 split, so this pins that the names it
reaches into the pipeline (config_ops.list_proxmox_templates, constants, the CLI flags it
builds) still resolve."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

REPO = Path(__file__).resolve().parent.parent


def _load_server():
    spec = importlib.util.spec_from_file_location("webui_server_routes", REPO / "webui" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Proc:
    def __init__(self, rc=None):
        self.rc = rc

    def poll(self):
        return self.rc


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class WebUIRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (root / "competitions").mkdir()
        cat = root / "catalog.json"
        cat.write_text(json.dumps([{"name": "mysql", "platform": "linux",
                                    "category": "service", "script": "x"}]))
        env = patch.dict(os.environ, {"TEZ_WEBUI_CATALOG_FILE": str(cat),
                                      "TF_VAR_proxmox_endpoint": "",
                                      "TF_VAR_proxmox_api_token": ""})
        env.start()
        self.addCleanup(env.stop)
        self.server = _load_server()
        self.server.COMPS = root / "competitions"
        self.server.JOB_LOG_DIR = root / "logs"
        self.server.REPO = root  # nodes.json lookup and job cwd
        self.client = TestClient(self.server.app)
        self.assertEqual(self.client.post("/api/comps", json={"id": "t1", "name": "T"}).status_code, 200)
        self.client.post("/api/comps/t1/boxes", json={"name": "web01", "last_octet": 4,
                                                       "template": "base-ubuntu24.04-fix"})

    def test_read_routes(self):
        self.assertEqual([c["id"] for c in self.client.get("/api/comps").json()], ["t1"])
        self.assertEqual(self.client.get("/api/comps/t1").status_code, 200)
        self.assertEqual(self.client.get("/api/comps/nope").status_code, 404)
        self.assertEqual(self.client.get("/api/comps/t1/packet").status_code, 200)
        self.assertEqual(self.client.put("/api/comps/t1/packet", json={"body": "# hi\n"}).status_code, 200)
        self.assertEqual(self.client.get("/api/catalog").status_code, 200)
        self.assertEqual(self.client.get("/api/catalog/mysql").status_code, 200)
        self.assertEqual(self.client.get("/api/catalog/zzz").status_code, 404)

    def test_inject_get_put_delete(self):
        self.assertEqual(self.client.put("/api/comps/t1/injects/01-a",
                                         json={"meta": {"title": "A"}, "body": "b"}).status_code, 200)
        self.assertEqual(self.client.get("/api/comps/t1/injects/01-a").status_code, 200)
        self.assertEqual(self.client.delete("/api/comps/t1/injects/01-a").status_code, 200)

    def test_templates_use_config_ops_when_live(self):
        r = self.client.get("/api/templates").json()
        self.assertIn("base-ubuntu24.04-fix", [t["name"] for t in r])
        with patch.dict(os.environ, {"TF_VAR_proxmox_endpoint": "https://x",
                                     "TF_VAR_proxmox_api_token": "t"}), \
                patch("config_ops.list_proxmox_templates", return_value=["live-tpl"]):
            r = self.client.get("/api/templates").json()
        self.assertTrue([t for t in r if t["name"] == "live-tpl" and t["live"]])

    def test_nodes(self):
        self.assertFalse(self.client.get("/api/nodes").json()["multi"])
        (self.server.REPO / "nodes.json").write_text(json.dumps(
            {"nodes": [{"name": "a", "node": "pve1", "datastore": "hdd"}]}))
        self.assertTrue(self.client.get("/api/nodes").json()["multi"])

    def test_deploy_verify_jobs_and_cli_flags_exist(self):
        started = []

        def popen(cmd, **kw):
            started.append(cmd)
            return _Proc(0)
        with patch.object(self.server.subprocess, "Popen", popen):
            r = self.client.post("/api/comps/t1/deploy", json={
                "teams": 2, "scoring_vmid": 950, "team_node": "a", "engine_node": "b",
                "plan_only": True})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self.client.post("/api/comps/t1/deploy", json={"teams": 99}).status_code, 400)
            v = self.client.post("/api/comps/t1/verify")
            self.assertEqual(v.status_code, 200, v.text)
        jobs = self.client.get("/api/comps/t1/jobs").json()
        self.assertEqual(len(jobs), 2)
        got = self.client.get(f"/api/jobs/{v.json()['id']}").json()
        self.assertIn("verify-competition.py", got["text"])
        self.assertEqual(self.client.get("/api/jobs/nope").status_code, 404)
        # every flag the server builds is a real argparse flag of the driver it launches
        helps = {s: subprocess.run([sys.executable, str(REPO / s), "--help"], capture_output=True,
                                   text=True, cwd=REPO).stdout
                 for s in ("create-competition.py",)}
        for flag in [a for a in started[0] if a.startswith("--")]:
            self.assertIn(flag, helps["create-competition.py"])
        self.assertTrue((REPO / started[1][1]).exists())


if __name__ == "__main__":
    unittest.main()
