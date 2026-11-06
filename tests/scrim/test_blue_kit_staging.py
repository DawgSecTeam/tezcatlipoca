"""The blue-kit staging: a competition's hardening kit rides into every blue workdir, the
cycle prompt tells blue it exists and how to use it without breaking the scored pins, and a
comp with no kit behaves exactly as before (release: the quetzalcoatl toolkit integration)."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path

from common.comp_paths import comps_root
from unittest import mock

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from scrim import blue_agent, blue_prompt, core  # noqa: E402
from estate import state as estate_state  # noqa: E402


def _args(run_dir, **kw):
    base = dict(competition="c1", teams=2, duration_min=90, blue_model="m", red_model="m",
                reasoning_effort="minimal", llm_base_url="https://openrouter.ai/api/v1",
                blue_base_url="https://openrouter.ai/api/v1", red_ip="10.0.0.198",
                run_dir=None if run_dir is None else str(run_dir), keep_range=False,
                keep_templates=False, blue_watchdog=False, resume_event=False,
                blue2_base_url=None,
                red_tunnel="auto", red_vmid=None)
    base.update(kw)
    return Namespace(**base)


CREDS = {"ENGINE_IP": "10.0.0.50", "ADMIN_PW": "adminpw", "INJECT_PW": "injpw", "BOX_PW": "boxpw",
         "BOX_USER": "obsadmin", "KEY_PATH": "/nonexistent/key", "VM_USER": "ops",
         "TEAM1_PW": "t1pw", "TEAM2_PW": "t2pw", "TEAM1_ID": "101", "TEAM2_ID": "102"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home"),
                                            "OPENROUTER_API_KEY": "fake-key-not-real"})
        home.start()
        self.addCleanup(home.stop)
        self.root = self.tmp / "repo"
        self.comp = self.root / "competitions" / "c1"
        self.comp.mkdir(parents=True)
        # A minimal comp: staging reads the state document, teams.json, credentials.txt and
        # (through the helpers) boxes.json; nothing else.
        (self.comp / "boxes.json").write_text(json.dumps(
            [{"name": "web01", "template": "base-debian13", "last_octet": 2}]))
        (self.comp / "box_services.json").write_text(json.dumps({"web01": ["ssh"]}))
        estate_state.write_flat(self.comp, {
            "admin_password": "adminpw", "box_password": "boxpw", "inject_password": "injpw",
            "box_creds": {"obsadmin": "boxpw"}})
        (self.comp / "teams.json").write_text(json.dumps(
            {"team1": {"password": "t1pw", "identifier": 101},
             "team2": {"password": "t2pw", "identifier": 102}}))
        (self.comp / "credentials.txt").write_text("Quotient http://10.0.0.50\n")
        (self.comp / "packet.md").write_text("# packet\n")
        (self.comp / "Compfile").write_text("name c1\n")
        p = mock.patch.object(core, "REPO", self.root)
        p.start()
        self.addCleanup(p.stop)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()


class BlueKitStaging(Base):
    def test_kit_staged_into_every_workdir_with_exec_bits(self):
        kit = self.root / "blue-kit"
        (kit / "baseline").mkdir(parents=True)
        (kit / "autofirewall.sh").write_text("#!/bin/sh\nexit 0\n")
        (kit / "autofirewall.sh").chmod(0o755)
        (kit / "baseline" / "standard.sh").write_text("#!/bin/sh\nexit 0\n")
        (kit / "baseline" / "standard.sh").chmod(0o755)
        (kit / "README.md").write_text("kit readme\n")
        args = _args(None)
        blue_agent.stage_blues(args, self.comp, self.run_dir, CREDS, time.time())
        for n in (1, 2):
            wd = self.run_dir / f"blue-team{n}"
            fd = wd / "blue-kit" / "autofirewall.sh"
            self.assertTrue(fd.exists(), f"blue-team{n} missing the kit")
            self.assertEqual(oct(fd.stat().st_mode & 0o777), "0o755",
                             "kit scripts must arrive runnable")
            self.assertEqual((wd / "blue-kit" / "README.md").read_text(), "kit readme\n")

    def test_no_kit_degrades_to_today_behavior(self):
        args = _args(None)
        blue_agent.stage_blues(args, self.comp, self.run_dir, CREDS, time.time())
        for n in (1, 2):
            self.assertFalse((self.run_dir / f"blue-team{n}" / "blue-kit").exists())

    def test_prompt_names_the_kit_when_present_and_not_when_absent(self):
        kit = self.root / "blue-kit"
        kit.mkdir()
        (kit / "autofirewall.sh").write_text("#!/bin/sh\nexit 0\n")
        args = _args(self.run_dir, comp_dir=self.comp)
        p = blue_prompt.blue_cycle_prompt(1, CREDS, args, 5, 85, "delta", "inj", "nb", "tail")
        self.assertIn("HARDENING KIT", p)
        self.assertIn("./scorch", p)
        shutil.rmtree(kit)
        p = blue_prompt.blue_cycle_prompt(1, CREDS, args, 5, 85, "delta", "inj", "nb", "tail")
        self.assertNotIn("HARDENING KIT", p)


if __name__ == "__main__":
    unittest.main()

