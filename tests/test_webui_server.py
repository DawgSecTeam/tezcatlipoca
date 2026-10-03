"""webui/server.py edits competitions/<id>/ in the files' own idiom: box_services.json /
box_vulns.json entries stay bare names unless they carry more than a name, a box rename carries
its pins, and ids/slugs can't escape the comp dir. Offline (temp competitions/ root, fixture
catalog). Skipped when the web UI's deps (fastapi, httpx) aren't installed."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ImportError:  # web UI deps are optional for the pipeline itself
    TestClient = None

REPO = Path(__file__).resolve().parent.parent


def _load_server():
    spec = importlib.util.spec_from_file_location("webui_server", REPO / "webui" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed (pip install -r webui/requirements.txt)")
class WebUIServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.comps = root / "competitions"
        self.comps.mkdir()
        catalog = root / "catalog.json"
        catalog.write_text(json.dumps([
            {"name": "mysql", "platform": "linux", "category": "service", "script": "x"},
            {"name": "suid-find", "platform": "linux", "category": "misconfiguration",
             "script": "chmod u+s find"},
            {"name": "hosts-redirect-linux", "platform": "linux",
             "category": "misconfiguration", "script": 'echo "$IP $HOSTS"'},
            {"name": "weak-password-policy-win", "platform": "windows",
             "category": "misconfiguration", "script": "net accounts"},
        ]))
        env = patch.dict(os.environ, {"TEZ_WEBUI_CATALOG_FILE": str(catalog),
                                      "TF_VAR_proxmox_endpoint": ""})
        env.start()
        self.addCleanup(env.stop)
        self.server = _load_server()
        self.server.COMPS = self.comps
        self.client = TestClient(self.server.app)
        r = self.client.post("/api/comps", json={"id": "t1", "name": "Test One"})
        self.assertEqual(r.status_code, 200, r.text)

    def tearDown(self):
        self.tmp.cleanup()

    def _read(self, name):
        return json.loads((self.comps / "t1" / name).read_text())

    def test_box_and_pins_round_trip_in_file_idiom(self):
        r = self.client.post("/api/comps/t1/boxes",
                             json={"name": "web01", "last_octet": 4,
                                   "template": "base-ubuntu24.04-fix"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.put("/api/comps/t1/boxes/web01/misconfigs", json=[
            "suid-find",
            {"name": "hosts-redirect-linux", "vars": {"HOSTS": "portal.corp"}},
            {"name": "plain", "vars": {}},  # nothing but a name → written back bare
        ])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._read("box_vulns.json")["web01"], [
            "suid-find", {"name": "hosts-redirect-linux", "vars": {"HOSTS": "portal.corp"}},
            "plain"])
        r = self.client.put("/api/comps/t1/boxes/web01/services",
                            json=[{"name": "mysql", "display": "sql", "port": "3306",
                                   "score_only": False}])
        self.assertEqual(self._read("box_services.json")["web01"],
                         [{"name": "mysql", "display": "sql", "port": 3306}])

    def test_rename_carries_pins_and_delete_drops_them(self):
        self.client.post("/api/comps/t1/boxes", json={"name": "db01", "last_octet": 5,
                                                     "template": "base-ubuntu24.04-fix"})
        self.client.put("/api/comps/t1/boxes/db01/misconfigs", json=["suid-find"])
        r = self.client.put("/api/comps/t1/boxes/db01", json={"name": "db02"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._read("box_vulns.json"), {"db02": ["suid-find"]})
        self.client.delete("/api/comps/t1/boxes/db02")
        self.assertEqual(self._read("boxes.json"), [])
        self.assertEqual(self._read("box_vulns.json"), {})

    def test_box_validation(self):
        ok = {"name": "web01", "last_octet": 4, "template": "base-ubuntu24.04-fix"}
        self.client.post("/api/comps/t1/boxes", json=ok)
        self.assertEqual(self.client.post("/api/comps/t1/boxes",
                                          json={**ok, "name": "web02"}).status_code, 409)
        self.assertEqual(self.client.post("/api/comps/t1/boxes",
                                          json={**ok, "name": "x", "last_octet": 1}).status_code, 400)
        self.assertEqual(self.client.post("/api/comps/t1/boxes",
                                          json={**ok, "name": "Bad Name"}).status_code, 400)

    def test_ids_and_slugs_cannot_escape(self):
        for bad in ("../etc", "..", "a/b", ".hidden", ""):
            with self.assertRaises(self.server.HTTPException):
                self.server.comp_dir(bad)
        self.assertEqual(self.client.get("/api/comps/.hidden").status_code, 400)
        for bad in ("..", "../x", ".git"):
            with self.assertRaises(self.server.HTTPException):
                self.server.inject_path(self.comps / "t1", bad)

    def test_inject_round_trip_and_offset_order(self):
        meta = {"title": "Hi", "open_offset_min": 0, "due_offset_min": 30, "close_offset_min": 60}
        r = self.client.put("/api/comps/t1/injects/01-hi", json={"meta": meta, "body": "# Hi\n"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((self.comps / "t1/injects/01-hi/briefing.md").read_text(), "# Hi\n")
        self.assertEqual(self.client.get("/api/comps/t1").json()["injects"], 1)
        bad = {**meta, "due_offset_min": 90}
        self.assertEqual(self.client.put("/api/comps/t1/injects/02-x",
                                         json={"meta": bad, "body": ""}).status_code, 400)

    def test_compfile_keeps_unknown_keys(self):
        (self.comps / "t1/Compfile").write_text("name A\nquotient_ref abc123\n")
        self.client.put("/api/comps/t1/compfile",
                        json={"items": [["name", "B"], ["quotient_ref", "abc123"]]})
        self.assertEqual((self.comps / "t1/Compfile").read_text(), "name B\nquotient_ref abc123\n")

    def test_catalog_filters_by_platform_and_kind(self):
        names = lambda q: [r["name"] for r in self.client.get(f"/api/catalog?{q}").json()]
        self.assertEqual(names("platform=linux&kind=services"), ["mysql"])
        self.assertEqual(sorted(names("platform=linux&kind=misconfigs")),
                         ["hosts-redirect-linux", "suid-find"])
        self.assertEqual(names("platform=windows&kind=misconfigs"), ["weak-password-policy-win"])
        entry = self.client.get("/api/catalog/hosts-redirect-linux").json()
        self.assertIn("HOSTS", entry["required_vars"])
        self.assertEqual(entry["identity_vars"], ["IP"])


if __name__ == "__main__":
    unittest.main()
