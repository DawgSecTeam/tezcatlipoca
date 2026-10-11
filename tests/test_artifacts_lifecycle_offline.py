"""Artifact lifecycle driven end to end, offline, on a synthetic competition directory.

collect_for_teardown (fake scp transport) -> test-artifacts.py list/show/plan/collect --dry-run/
verify/archive/verify --seal through the real CLI subprocess. No Proxmox, no network."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import artifacts_ops  # noqa: E402

RUN_ID = "run-deadbeef"


def _no_stage(*a, **k):
    '''The offline contract: never touch the network, not even to stage red's files.'''
    return False


def _fake_transport():
    """scp-jump: red01 has the report + events but no world.json; the ssh-cmd channel is dead."""
    from artifacts_lib.constants import Unreachable

    def scp(ssh, remote, dest_dir, timeout=120):
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if remote.endswith("report-*.md"):
            out = dest_dir / "report-1.md"
            out.write_text("# red report\nowned web01\n")
        elif remote.endswith("events.jsonl"):
            out = dest_dir / "events.jsonl"
            out.write_text('{"e": 1}\n')
        else:
            raise FileNotFoundError(remote)
        os.chmod(out, 0o600)
        return [out]

    def ssh_cmd(ssh, cmd, dest, timeout=90):
        raise Unreachable("journal channel dead")

    return {"scp-jump": scp, "ssh-cmd": ssh_cmd,
            "local": artifacts_ops.default_transport()["local"]}


class ArtifactLifecycleOffline(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.comp = root / "competitions" / "synth"
        self.comp.mkdir(parents=True)
        (self.comp / "Compfile").write_text("name synth\nscenario s\ndifficulty 1\n")
        (self.comp / ".deploy_state.json").write_text(json.dumps({"run_id": RUN_ID}))
        self.run_dir = root / "run"
        self.run_dir.mkdir()
        (self.run_dir / "run.json").write_text('{"phase": "done"}')
        (self.run_dir / "T0.txt").write_text("2026-10-04T00:00:00Z\n")
        (self.run_dir / "INTERACTION.md").write_text("# interaction\n")
        self.blue = root / "blue1"
        self.blue.mkdir()
        (self.blue / "REPORT.md").write_text("# blue report\nrotated creds\n")
        (self.blue / "LOG.md").write_text("log\n")
        (self.blue / "cycles").mkdir()
        (self.blue / "cycles" / "c1.md").write_text("cycle\n")
        self.archive = root / "archive"
        self.env = dict(os.environ, TEZ_ARTIFACTS_ARCHIVE=str(self.archive))

        path, _ = artifacts_ops.ensure_test(self.comp, kind="scrim", run_id=RUN_ID,
                                            script="synthetic", teams=2, boxes=["web01"])
        artifacts_ops.update_manifest(path, agents={"red": {"present": True, "ssh": {"host": "x"}},
                                                    "blue": {"present": True}})
        artifacts_ops.record_paths(path, run_dir=self.run_dir, blue_workdirs=[self.blue])
        self.path = path

    def cli(self, *argv, ok=(0,)):
        proc = subprocess.run([sys.executable, str(_REPO / "test-artifacts.py"), *argv],
                              capture_output=True, text=True, cwd=str(_REPO), env=self.env)
        self.assertIn(proc.returncode, ok, proc.stdout + proc.stderr)
        return proc

    def test_full_lifecycle(self):
        lines = []
        collection = artifacts_ops.collect_for_teardown(
            self.comp, run_id=RUN_ID, transport=_fake_transport(), echo=lines.append,
            stage_red=_no_stage)
        self.assertIn("summary", collection)
        for rel in ("RED-TEAM.md", "BLUE-TEAM.md", "REPORT.md", "collection.json",
                    "evidence/red/events.jsonl", "evidence/harness/run.json"):
            self.assertTrue((self.path / rel).exists(), rel)
        self.assertIn("rotated creds", (self.path / "BLUE-TEAM.md").read_text())
        self.assertIn("owned web01", (self.path / "RED-TEAM.md").read_text())
        # the dead world.json / journal were recorded, not dropped
        statuses = json.dumps(collection)
        self.assertIn("absent", statuses)
        self.assertIn("unreachable", statuses)
        mode = (self.path / "evidence/red/events.jsonl").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

        listed = json.loads(self.cli("list", str(self.comp), "--json").stdout)
        self.assertEqual([t["key"] for t in listed["tests"]], [RUN_ID])
        self.cli("list", str(self.comp))
        shown = json.loads(self.cli("show", str(self.comp), RUN_ID, "--json").stdout)
        self.assertTrue(shown)
        self.cli("show", str(self.comp), RUN_ID, ok=(0, 1))
        self.cli("plan", str(self.comp), RUN_ID)

        # collect --dry-run contacts nobody and must not clobber the real files
        before = (self.path / "evidence/red/events.jsonl").read_text()
        self.cli("collect", str(self.comp), RUN_ID, "--dry-run", ok=(0, 1))
        self.assertEqual((self.path / "evidence/red/events.jsonl").read_text(), before)

        self.cli("verify", str(self.comp), RUN_ID, ok=(0, 1))
        # TODO(author) markers remain -> seal refused, no archive of a skeleton as "done"
        refused = self.cli("verify", str(self.comp), RUN_ID, "--seal", ok=(1,))
        self.assertIn("TODO", refused.stdout + refused.stderr)
        self.assertEqual(artifacts_ops.load_manifest(self.path)["writeup"]["status"],
                         "needs-writeup")

        arch = self.cli("archive", str(self.comp), "--all")
        self.assertTrue((self.archive / "synth" / RUN_ID / "test.json").exists(), arch.stdout)

        # author fills the judgement sections -> seal succeeds
        report = self.path / "REPORT.md"
        import re
        text = report.read_text()
        self.assertIn("test-artifacts.py verify --seal", text)  # the front-matter hint names a real command
        report.write_text(re.sub(r"<!-- TODO\(author\).*?-->", "done.", text, flags=re.S))
        self.assertNotIn("TODO(author)", report.read_text())
        self.cli("verify", str(self.comp), RUN_ID, "--seal", "--author", "tester")
        self.assertEqual(artifacts_ops.load_manifest(self.path)["writeup"]["status"], "done")
        self.assertTrue((self.archive / "synth" / RUN_ID / "REPORT.md").exists())

    def test_teardown_twice_does_not_split_folder_or_overwrite_report(self):
        artifacts_ops.collect_for_teardown(self.comp, run_id=RUN_ID, stage_red=_no_stage,
                                           transport=_fake_transport(), echo=lambda *_: None)
        report = self.path / "REPORT.md"
        report.write_text("hand written\n")
        artifacts_ops.collect_for_teardown(self.comp, run_id=RUN_ID, stage_red=_no_stage,
                                           transport=_fake_transport(), echo=lambda *_: None)
        self.assertEqual(report.read_text(), "hand written\n")
        keys = [p.name for p in (self.comp / ".automated-tests").iterdir() if p.is_dir()]
        self.assertEqual(keys, [RUN_ID])

    def test_dead_boxes_never_block(self):
        from artifacts_lib.constants import Unreachable

        def dead(*a, **k):
            raise Unreachable("down")
        coll = artifacts_ops.collect_for_teardown(
            self.comp, run_id=RUN_ID, stage_red=_no_stage,
            transport={"scp-jump": dead, "ssh-cmd": dead,
                                                 "local": artifacts_ops.default_transport()["local"]},
            echo=lambda *_: None)
        self.assertIn("unreachable", json.dumps(coll))
        self.assertIn("unreachable", (self.path / "RED-TEAM.md").read_text().lower())


if __name__ == "__main__":
    unittest.main()
