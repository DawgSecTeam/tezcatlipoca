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
    def test_linux_skip_block_guards_every_destructive_line(self):
    # The decoy kill (2026-10-11): a planted path matching a staging rm glob
    #         died in the wipe. Every rm/truncate in LINUX_SCRIPT must go through
    #         in_skip, and the skip block must define it before any use.
        block = clean_trails._linux_skip_block(["/tmp/.imix-decoy", "/opt/plant"])
        script = block + clean_trails.LINUX_SCRIPT
        self.assertIn("SKIP_PATHS=", block)
        self.assertIn("in_skip() {", block)
        self.assertNotIn("\nrm -rf", script.replace("rm -rf \"$p\"", ""))
        self.assertIn('! in_skip "$p"', script)
        self.assertIn('! in_skip "$f"', script)
        self.assertIn('! in_skip "$h"', script)
        # the heredoc is quoted, so a path is data, never shell to execute
        self.assertIn("<<'TZ_SKIP'", block)

class TestSkipList(unittest.TestCase):
    def test_extract_paths_covers_paths_and_detail_tokens(self):
        arts = [
            {"paths": ["/etc/systemd/system/x.service"], "detail": "persist x"},
            {"paths": [], "detail": "imix[grpc] /usr/local/lib/.x-cache -> "
                                    "172.31.120.1:8000 (sshd-timer.service)"},
            {"paths": ["bad\npath"], "detail": ""},
        ]
        paths = clean_trails.extract_artifact_paths(arts)
        self.assertIn("/etc/systemd/system/x.service", paths)
        self.assertIn("/usr/local/lib/.x-cache", paths)
        self.assertNotIn("bad\npath", paths)              # newline paths dropped

    def test_red01_world_artifacts_reads_the_deploy_state_record(self):
        comp = Path(tempfile.mkdtemp())
        (comp / ".deploy_state.json").write_text(json.dumps(
            {"assume_breach": {"red_ip": "10.0.0.198"}}))
        creds = {"KEY_PATH": "/k"}
        world = {"artifacts": [{"ip": "192.168.120.2", "kind": "realm-imix",
                                "detail": "imix[grpc] /usr/local/lib/.x-cache",
                                "check_cmd": "pgrep -f x"}]}
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["timeout"] = kwargs.get("timeout")
            return SimpleNamespace(returncode=0, stdout=json.dumps(world), stderr="")

        with mock.patch.object(clean_trails.procs, "run_tree", fake_run):
            arts, red_ip = clean_trails.red01_world_artifacts(comp, creds)
        self.assertEqual(red_ip, "10.0.0.198")
        self.assertEqual(len(arts), 1)
        self.assertIn("sysadmin@10.0.0.198", seen["argv"])
        self.assertIn("world.json", seen["argv"][-1])

    def test_red01_unavailable_is_empty_not_fatal(self):
        comp = Path(tempfile.mkdtemp())                   # no deploy state at all
        arts, red_ip = clean_trails.red01_world_artifacts(comp, {"KEY_PATH": "/k"})
        self.assertEqual((arts, red_ip), ([], None))


class TestIntegrity(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.comp = _comp(self.root)
        self.creds = {"KEY_PATH": "/k", "VM_USER": "jump", "ENGINE_IP": "10.0.0.252",
                      "BOX_USER": "medic", "BOX_PW": "pw"}
        self.arts = [
            {"ip": "192.168.120.3", "kind": "realm-imix", "detail": "d1",
             "check_cmd": "pgrep -f beacon"},
            {"ip": "192.168.120.3", "kind": "persistence_linux", "detail": "d2",
             "check_cmd": "test -f /etc/systemd/system/p.service"},
        ]

    def test_linux_script_reports_per_artifact_rcs(self):
        script = clean_trails._linux_integrity_script(self.arts)
        self.assertIn("pgrep -f beacon", script)
        self.assertIn('echo "INTEG|0|$?"', script)
        self.assertIn('echo "INTEG|1|$?"', script)

    def test_windows_script_probes_recorded_paths(self):
        out = clean_trails._windows_integrity_script(
            [{"ip": "x", "kind": "k", "paths": [r"C:\ProgramData\p.exe"]},
             {"ip": "x", "kind": "k2", "paths": []}])
        self.assertIn("Test-Path -LiteralPath 'C:\\ProgramData\\p.exe'", out)
        self.assertIn("INTEG|0|0", out)
        self.assertIn("INTEG|1|-1", out)                  # uncheckable, not dead

    def test_casualty_is_alive_before_dead_after(self):
        pre = {"192.168.120.3": [(0, "realm-imix", 0), (1, "persistence_linux", 0)]}
        post = {"192.168.120.3": [(0, "realm-imix", 1), (1, "persistence_linux", 0)]}
        alive, checked, casualties = clean_trails._integrity_report(
            pre, post, self.arts, "post")
        self.assertEqual((alive, checked), (1, 2))
        self.assertEqual(len(casualties), 1)
        self.assertEqual(casualties[0][0], "192.168.120.3")
        self.assertEqual(casualties[0][1], "realm-imix")

    def test_clean_trails_runs_the_pre_and_post_check(self):
    # The wipe must re-run the artifact checks around itself and name
    #         anything that died in the window (the vanished decoy, 2026-10-11).
        checks = {"n": 0}

        def fake_integrity(comp, creds, artifacts):
            checks["n"] += 1
            if checks["n"] == 1:                          # pre-wipe: both alive
                return {"192.168.120.2": [(0, "k", 0)],
                        "192.168.120.3": [(0, "realm-imix", 0)]}
            return {"192.168.120.2": [(0, "k", 0)],
                    "192.168.120.3": [(0, "realm-imix", 1)]}

        with mock.patch.object(clean_trails, "plant_integrity", fake_integrity), \
                mock.patch.object(clean_trails, "red01_world_artifacts",
                                  return_value=([{"ip": "192.168.120.3",
                                                  "kind": "realm-imix",
                                                  "detail": "imix[grpc] /a/b",
                                                  "check_cmd": "pgrep -f x"}],
                                                "10.0.0.198")), \
                mock.patch.object(clean_trails, "_clean_linux",
                                  lambda *a, **k: (True, "")), \
                mock.patch.object(clean_trails, "_clean_windows",
                                  lambda *a, **k: (True, "")), \
                mock.patch.object(clean_trails, "box_targets",
                                  return_value=[{"ip": "192.168.120.2",
                                                 "name": "dc01", "windows": True},
                                                {"ip": "192.168.120.3",
                                                 "name": "web01", "windows": False}]):
            res = clean_trails.clean_trails(self.comp, self.creds)
        self.assertTrue(all(res.values()))
        self.assertEqual(checks["n"], 2)                  # pre + post

class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.comp = _comp(self.root)

    def test_dispatch_and_result_map(self):
        calls = []
        with mock.patch.object(clean_trails, "_clean_linux",
                               lambda *a, **k: calls.append(("linux", a[1])) or (True, "")), \
                mock.patch.object(clean_trails, "_clean_windows",
                                  lambda *a, **k: calls.append(("win", a[1])) or (True, "")):
            res = clean_trails.clean_trails(self.comp, {})
        self.assertEqual(sorted(kind for kind, _ in calls), ["linux", "win"])
        self.assertEqual(len(res), 2)
        self.assertTrue(all(res.values()))
        self.assertEqual(sorted(res), ["192.168.120.2", "192.168.120.3"])

    def test_a_failing_box_is_reported_not_raised(self):
        def boom(*a, **k):
            raise RuntimeError("no route to host")
        with mock.patch.object(clean_trails, "_clean_linux", boom), \
                mock.patch.object(clean_trails, "_clean_windows",
                                  lambda *a, **k: False):
            res = clean_trails.clean_trails(self.comp, {})
        self.assertEqual(len(res), 2)
        self.assertFalse(any(res.values()))     # a wipe failure never aborts a deploy


if __name__ == "__main__":
    unittest.main()


class TestTeardownStaging(unittest.TestCase):
# The teardown path must stage red's root-owned artifacts too.
#
#     collect_for_teardown is the collector for the run whose harness died, so this is
#     the one path where red's record exists nowhere else — and with the plan reading
#     /tmp/ba it needs the staging step, which the harness happens to do for itself.

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
