"""Trail wipe: the deploy must not leave blue a readable copy of the plant.

The tezcatlipoca deploy, the nakon plant and the day-0 seed all reach each box
over ssh as root, so the box's own logs (auth.log, the journal, apt history, the
shell histories, the Windows SCM event log + PowerShell history) describe the
whole operation. Blue's 2026-10-08 note already listed every planted unit and the
key comment, so the wipe is what keeps the randomized naming from being undone by
a single `grep`.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scrim import clean_trails


def _comp(root, windows=("dc01",), boxes=("dc01", "web01", "fw01")):
    comp = Path(root) / "demo"
    comp.mkdir(parents=True)
    (comp / "targets.json").write_text(json.dumps({"targets": {
        f"team1-{b}": {"team_key": "team1", "identifier": "120", "box_name": b,
                       "vmid": 1400 + i, "ip": f"192.168.120.{2 + i}"}
        for i, b in enumerate(boxes)}}))
    (comp / "boxes.json").write_text(json.dumps([
        {"name": b, "last_octet": 2 + i,
         "template": "base-windows-server" if b in windows else
                     ("pfsense-provision" if b == "fw01" else "base-debian13-lite-fix"),
         **(dict(unmanaged=True, in_path=True) if b == "fw01" else {})}
        for i, b in enumerate(boxes)]))
    return comp


class TestTargets(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.comp = _comp(self.root)

    def test_firewall_is_skipped_and_windows_marked(self):
        targets = clean_trails.box_targets(self.comp, {})
        names = {t["name"] for t in targets}
        self.assertEqual(names, {"dc01", "web01"})     # the pfSense appliance has no shell
        by_name = {t["name"]: t for t in targets}
        self.assertTrue(by_name["dc01"]["windows"])
        self.assertFalse(by_name["web01"]["windows"])

    def test_creds_for_deploy_shape(self):
        ctx = SimpleNamespace(ssh_key_abs="/k", engine_mgmt_ip="10.0.0.252",
                              box_username="medic", box_password="pw")
        creds = clean_trails.creds_for_deploy(ctx)
        self.assertEqual(creds["KEY_PATH"], "/k")
        self.assertEqual(creds["ENGINE_IP"], "10.0.0.252")
        self.assertEqual(creds["BOX_USER"], "medic")
        self.assertTrue(creds["VM_USER"])       # the engine jump user


class TestScripts(unittest.TestCase):
    def test_linux_script_covers_the_sources_blue_reads(self):
        for needle in ("auth.log", "syslog", "apt/history.log", "dpkg.log",
                       "wtmp", ".bash_history", "journalctl --vacuum", "/tmp/ba"):
            self.assertIn(needle, clean_trails.LINUX_SCRIPT)
        self.assertIn("TRAILS_LINUX_OK", clean_trails.LINUX_SCRIPT)

    def test_windows_script_covers_payload_and_event_sources(self):
        for needle in ("Panther", "Setup\\Scripts", "ConsoleHost_history",
                       "System32\\LogFiles", "wevtutil"):
            self.assertIn(needle, clean_trails.WINDOWS_PS)
        self.assertIn("TRAILS_WIN_OK", clean_trails.WINDOWS_PS)


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.comp = _comp(self.root)

    def test_dispatch_and_result_map(self):
        calls = []
        with mock.patch.object(clean_trails, "_clean_linux",
                               lambda c, ip: calls.append(("linux", ip)) or True), \
                mock.patch.object(clean_trails, "_clean_windows",
                                  lambda c, ip: calls.append(("win", ip)) or True):
            res = clean_trails.clean_trails(self.comp, {})
        self.assertEqual(sorted(kind for kind, _ in calls), ["linux", "win"])
        self.assertEqual(len(res), 2)
        self.assertTrue(all(res.values()))
        self.assertEqual(sorted(res), ["192.168.120.2", "192.168.120.3"])

    def test_a_failing_box_is_reported_not_raised(self):
        def boom(creds, ip):
            raise RuntimeError("no route to host")
        with mock.patch.object(clean_trails, "_clean_linux", boom), \
                mock.patch.object(clean_trails, "_clean_windows", lambda c, ip: False):
            res = clean_trails.clean_trails(self.comp, {})
        self.assertEqual(len(res), 2)
        self.assertFalse(any(res.values()))     # a wipe failure never aborts a deploy


class TestPlanReadsStagedCopies(unittest.TestCase):
    def test_red_wants_are_the_staged_paths(self):
        """All three red artifacts are 0600 root in /var/lib/bad-auto: the collector
        has to read the sudo-staged copies, or one unreadable file marks the target
        unreachable and takes the others with it."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import artifacts_ops as ao

        comp = _comp(tempfile.mkdtemp())
        test_dir = comp / ao.DIRNAME / "run-abc12345"
        test_dir.mkdir(parents=True)
        ao.ensure_test(comp, kind="scrim", key="run-abc12345",
                       run_id="run-abc12345", script="run-agent-scrim.py")
        ao.update_manifest(test_dir, agents={"red": {"present": True,
                                                    "ssh": {"host": "10.0.0.198"}}})
        targets = ao.plan_targets(ao.load_manifest(test_dir), comp_dir=comp)
        red = next(t for t in targets if t["name"] == "red01")
        remotes = [w.get("remote") for w in red["want"] if w.get("remote")]
        self.assertTrue(remotes)
        for remote in remotes:
            self.assertTrue(remote.startswith("/tmp/ba/"), f"{remote} is not a staged copy")


if __name__ == "__main__":
    unittest.main()


class TestTeardownStaging(unittest.TestCase):
    """The teardown path must stage red's root-owned artifacts too.

    collect_for_teardown is the collector for the run whose harness died, so this is
    the one path where red's record exists nowhere else — and with the plan reading
    /tmp/ba it needs the staging step, which the harness happens to do for itself."""

    def test_no_red_agent_means_nothing_to_stage(self):
        from artifacts_lib import lifecycle
        self.assertFalse(lifecycle.stage_red_artifacts({}))

    def test_staging_is_attempted_from_the_manifest_ssh_record(self):
        from artifacts_lib import lifecycle
        manifest = {"agents": {"red": {"present": True,
                                       "ssh": {"host": "10.0.0.198", "user": "sysadmin",
                                               "key": "/k", "jump": "ProxyCommand=x"}}}}
        argv_seen = {}

        def fake_run(argv, **kwargs):
            argv_seen["argv"] = argv
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", fake_run):
            self.assertTrue(lifecycle.stage_red_artifacts(manifest))
        self.assertIn("sysadmin@10.0.0.198", argv_seen["argv"])
        self.assertIn("ProxyCommand=x", argv_seen["argv"])
        self.assertTrue(any("sudo -n cp /var/lib/bad-auto/world.json" in str(a)
                            for a in argv_seen["argv"]))
