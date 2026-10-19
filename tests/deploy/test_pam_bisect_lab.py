"""The PAM bisect lab and its support-bundle collector, offline.

Covers the pieces that must be right before an event: the command set a bundle promises,
the guest-agent binding and 0600/0700 handling, the no-secrets-on-stdout rule, the bisect
config-set parser and its correlation verdict, and the CLI shape. No Proxmox, no SSH."""

import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from common import pam_bundle  # noqa: E402


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "pam_bisect_lab", _REPO / "tools" / "pam-bisect-lab.py")
    module = importlib.util.module_from_spec(spec)
    # The tool loads the real .env at import; the offline suite must not inherit it.
    with patch("dotenv.load_dotenv"):
        spec.loader.exec_module(module)
    return module


tool = _load_tool()


class CollectorShapeTests(unittest.TestCase):
    def test_bundle_covers_the_required_pam_and_ssh_evidence(self):
        script = "\n".join(command for _name, command in pam_bundle.BUNDLE_COMMANDS)
        for needle in ("/etc/pam.d/*", "/etc/security/*", "/etc/nsswitch.conf",
                       "/etc/ssh/sshd_config", "sshd -T", "getent passwd", "getent shadow",
                       "stat -c", "faillock", "pam_tally2", "journalctl -u ssh",
                       "/var/log/auth.log", "systemd-logind", "dpkg -l"):
            self.assertIn(needle, script)
        names = [name for name, _command in pam_bundle.BUNDLE_COMMANDS]
        self.assertEqual(names, list(dict.fromkeys(names)))

    def test_capture_binds_the_guest_agent_and_writes_private_files(self):
        scripts = []

        def fake_exec(node, vmid, script, timeout=0):
            scripts.append(script)
            return 0, "output for " + script.split()[0], ""

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(pam_bundle, "guest_agent_exec_root", side_effect=fake_exec):
            result = pam_bundle.capture_pam_bundle(
                "pve", 2104, Path(tmp) / "bundle", metadata={"label": "unit"})
            out = Path(tmp) / "bundle"
            self.assertEqual(result["captured"], len(pam_bundle.BUNDLE_COMMANDS))
            self.assertEqual(len(scripts), len(pam_bundle.BUNDLE_COMMANDS))
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
            for path in out.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
            manifest = json.loads((out / "bundle.json").read_text())
            self.assertEqual(manifest["metadata"]["vmid"], 2104)
            self.assertEqual(manifest["metadata"]["label"], "unit")
            self.assertEqual(manifest["metadata"]["node"], "pve")
            self.assertIn("getent shadow", (out / "shadow.txt").read_text())

    def test_section_output_never_reaches_stdout(self):
        def fake_exec(node, vmid, script, timeout=0):
            return 0, "SECRET-SHADOW-HASH", ""

        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with patch.object(pam_bundle, "guest_agent_exec_root", side_effect=fake_exec), \
                 redirect_stdout(buf):
                result = pam_bundle.capture_pam_bundle("pve", 1, Path(tmp) / "bundle")
            self.assertIn("SECRET-SHADOW-HASH",
                          (Path(tmp) / "bundle" / "shadow.txt").read_text())
            self.assertNotIn("SECRET-SHADOW-HASH", buf.getvalue())
            self.assertNotIn("SECRET-SHADOW-HASH", str(result))

    def test_lost_channel_is_recorded_and_stops_the_run(self):
        calls = []

        def fake_run(script, timeout):
            calls.append(script)
            raise RuntimeError("guest agent restarted")

        with tempfile.TemporaryDirectory() as tmp:
            result = pam_bundle.collect_bundle(fake_run, Path(tmp) / "bundle", timeout=1)
            manifest = json.loads((Path(tmp) / "bundle" / "bundle.json").read_text())
        self.assertEqual(result["captured"], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result["entries"]), len(pam_bundle.BUNDLE_COMMANDS))
        self.assertEqual(result["entries"][0]["status"], "error")
        self.assertEqual(result["entries"][1]["status"], "skipped")
        self.assertEqual(manifest["entries"][0]["status"], "error")


class BisectClassifierTests(unittest.TestCase):
    def _write_configs(self, raw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "configs.json"
        path.write_text(json.dumps(raw))
        return path

    def test_config_set_parses_name_and_omit(self):
        path = self._write_configs([
            {"name": "pam-auth-faillock", "omit": "rm -f /etc/pam.d/common-auth"},
            {"name": "security-limits", "omit": "rm -f /etc/security/limits.conf"},
        ])
        self.assertEqual(tool.load_configs(path), [
            {"name": "pam-auth-faillock", "omit": "rm -f /etc/pam.d/common-auth"},
            {"name": "security-limits", "omit": "rm -f /etc/security/limits.conf"},
        ])

    def test_malformed_config_sets_refuse(self):
        for raw in ([], [{"name": "x"}], ["pam-auth-faillock"], {"name": "x", "omit": "y"}):
            with self.subTest(raw=raw), self.assertRaises(SystemExit):
                tool.load_configs(self._write_configs(raw))

    def test_only_healthy_after_omission_correlates(self):
        rows = [{"name": "A", "verdict": "healthy"},
                {"name": "B", "verdict": "pam-trap"},
                {"name": "C", "verdict": "unreachable"},
                {"name": "D", "verdict": "omit-failed"}]
        self.assertEqual(tool.correlate(rows), ["A"])

    def test_bisect_omits_each_config_against_the_trap_baseline(self):
        configs = [{"name": "A", "omit": "a"}, {"name": "B", "omit": "b"},
                   {"name": "C", "omit": "c"}]
        retests = []

        def retest(cfg):
            retests.append(None if cfg is None else cfg["name"])
            if cfg is None:
                return "pam-trap"
            return {"A": "healthy", "B": "pam-trap", "C": "unreachable"}[cfg["name"]]

        result = tool.run_bisect(configs, retest, echo=lambda _msg: None)
        self.assertEqual(retests, [None, "A", "B", "C"])
        self.assertEqual(result["baseline"], "pam-trap")
        self.assertEqual(result["correlates"], ["A"])

    def test_no_correlation_without_a_reproducing_baseline(self):
        configs = [{"name": "A", "omit": "a"}]
        retests = []

        def retest(cfg):
            retests.append(cfg)
            return "healthy"

        result = tool.run_bisect(configs, retest, echo=lambda _msg: None)
        self.assertEqual(retests, [None])
        self.assertEqual(result["baseline"], "healthy")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["correlates"], [])


class ParserTests(unittest.TestCase):
    def test_every_subcommand_parses(self):
        parser = tool.build_parser()
        grab = parser.parse_args(["grab", "--node", "pve", "--vmid", "7", "--out", "/tmp/o"])
        self.assertEqual((grab.node, grab.vmid, grab.out), ("pve", 7, "/tmp/o"))
        up = parser.parse_args(["up", "--template", "planted-web", "--out", "/tmp/lab"])
        self.assertEqual((up.template, up.agent_timeout), ("planted-web", 300))
        cycle = parser.parse_args(["cycle", "--lab", "/tmp/lab/lab.json", "--cycles", "4"])
        self.assertEqual((cycle.cycles, cycle.user, cycle.settle), (4, "ubuntu", 20))
        bisect = parser.parse_args([
            "bisect", "--lab", "/tmp/lab/lab.json", "--snapshot", "tz-ready",
            "--configs", "/tmp/configs.json"])
        self.assertEqual((bisect.snapshot, bisect.cycles, bisect.omit_timeout),
                         ("tz-ready", 1, 60))
        down = parser.parse_args(["down", "--lab", "/tmp/lab/lab.json"])
        self.assertEqual(down.lab, "/tmp/lab/lab.json")

    def test_bisect_requires_snapshot_and_configs(self):
        with self.assertRaises(SystemExit):
            tool.build_parser().parse_args(["bisect", "--lab", "/tmp/lab/lab.json"])


class DownGuardTests(unittest.TestCase):
    def test_refuses_a_state_file_without_the_lab_ownership_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "lab.json"
            state.write_text(json.dumps(
                {"node": "pve", "vmid": 2104, "tags": ["tezcatlipoca", "comp-other"]}))
            with self.assertRaises(SystemExit):
                tool.cmd_down(SimpleNamespace(lab=str(state)))
            self.assertTrue(state.exists())
