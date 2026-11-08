"""A2 — the scrim must never enter an event with missing injects.

run-bd70a985: deploy --hold-start defers inject creation to competition-ops start,
the scrim's own T0 path walked past it, and reanchor_injects only WARNED — the
event ran 120 minutes with 0 injects. reanchor_injects now CREATES the missing
ones on the engine clock over the jar it already holds (Quotient's one-session-
per-account rule forbids a second admin login mid-staging)."""

import json
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from scrim import inject_sync  # noqa: E402

CREDS = {"ENGINE_IP": "10.0.0.50", "ADMIN_PW": "adminpw"}


def _inj(title, **kw):
    d = {"title": title, "description": f"desc {title}",
         "open_offset_min": 0, "due_offset_min": 60, "close_offset_min": 120,
         "open_time": "2026-11-08T18:00:00Z", "due_time": "2026-11-08T19:00:00Z",
         "close_time": "2026-11-08T20:00:00Z", "files": []}
    d.update(kw)
    return d


class _Run:
    """Curl stand-in: records argv, answers per-URL bodies."""

    def __init__(self, create_body='{"ID":7}'):
        self.cmds, self.create_body = [], create_body

    def __call__(self, cmd, capture_output=False, text=False):
        self.cmds.append(list(cmd))
        out = self.create_body if "/api/injects/create" in " ".join(cmd) else ""
        return SimpleNamespace(stdout=out, returncode=0)


def _reanchor(tmp, injects, remote, run_stub):
    args = Namespace(run_dir=str(tmp))
    comp = SimpleNamespace(name="c1")
    with mock.patch("config.injects_config.load_injects", return_value=injects), \
         mock.patch("config.injects_config.resolve_inject_times", lambda x: x), \
         mock.patch("scrim.inject_sync.quotient_api._qlogin"), \
         mock.patch("scrim.inject_sync.quotient_api.qget",
                    return_value=SimpleNamespace(stdout=json.dumps(remote))), \
         mock.patch("scrim.inject_sync.subprocess.run", run_stub):
        inject_sync.reanchor_injects(args, comp, CREDS)


class CreatesMissingInjects(unittest.TestCase):
    def test_missing_title_is_created_over_the_held_jar(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            stub = _Run()
            _reanchor(Path(td), [_inj("SMB walk")], [], stub)
            creates = [c for c in stub.cmds if "/api/injects/create" in " ".join(c)]
            self.assertEqual(len(creates), 1)
            argv = " ".join(creates[0])
            self.assertIn("-b", creates[0])                      # the held jar, never -c
            self.assertNotIn("-c ", argv)                        # no second admin login
            for field in ("title=SMB walk", "open-time=2026-11-08T18:00:00Z",
                          "due-time=2026-11-08T19:00:00Z", "close-time=2026-11-08T20:00:00Z"):
                self.assertIn(field, argv)

    def test_existing_titles_are_never_recreated(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            stub = _Run()
            remote = [{"Title": "SMB walk", "ID": 3, "InjectFileNames": []}]
            _reanchor(Path(td), [_inj("SMB walk")], remote, stub)
            self.assertFalse([c for c in stub.cmds if "/api/injects/create" in " ".join(c)])
            self.assertTrue([c for c in stub.cmds if "/api/injects/3" in " ".join(c)])

    def test_engine_refusal_warns_with_the_title(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            stub = _Run(create_body='{"error":"event already started"}')
            logged = []
            with mock.patch("scrim.inject_sync.log", logged.append):
                _reanchor(Path(td), [_inj("Beacon hunt")], [], stub)
            self.assertTrue(any("Beacon hunt" in m and "could not be created" in m
                                for m in logged), logged)

    def test_attachments_ride_as_files_at_paths(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            stub = _Run()
            _reanchor(Path(td), [_inj("Log review", files=["/x/one.log"])], [], stub)
            argv = " ".join(stub.cmds[0])
            self.assertIn("-F files=@/x/one.log", argv)


if __name__ == "__main__":
    unittest.main()
