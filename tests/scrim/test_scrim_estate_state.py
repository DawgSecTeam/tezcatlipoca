"""The scrim reads the competition's ONE state document, estate-state.json; nothing in scrim/ may reach for a second one."""

import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from scrim import core, red_stage  # noqa: E402

_DELETED_STATE_FILE = re.compile(r"\.deploy_state\.json")


class ReadState(unittest.TestCase):
    def test_it_reads_the_document(self):
        with tempfile.TemporaryDirectory() as td:
            comp = Path(td)
            (comp / "estate-state.json").write_text(
                '{"schema_version": 1, "identity": {"run_id": "run-1", "scoring_vm_id": 2101},'
                ' "secrets": {"box_creds": {"a": "b"}}, "run": {"last_phase": 8}}')
            state = core.read_state(comp)
        self.assertEqual(state["run_id"], "run-1")
        self.assertEqual(state["scoring_vm_id"], 2101)
        self.assertEqual(state["box_creds"], {"a": "b"})
        self.assertEqual(state["last_phase"], 8)

    def test_no_scrim_module_names_the_deleted_state_file(self):
        offenders = [f"{path.name}:{i}"
                     for path in Path(core.__file__).parent.glob("*.py")
                     for i, line in enumerate(path.read_text().splitlines(), 1)
                     if _DELETED_STATE_FILE.search(line)]
        self.assertEqual(offenders, [], "route state reads through core.read_state")


class RedIpResolution(unittest.TestCase):
    def test_the_scrim_carries_the_comps_red01_pin_into_the_rebuilt_config(self):
        with tempfile.TemporaryDirectory() as td:
            comp = Path(td)
            (comp / "Compfile").write_text("name c\nred01_ip 10.0.0.196\n")
            args = SimpleNamespace(red_ip=None)
            red_stage._resolve_red_ip(args, comp, None)
            self.assertEqual(args.red_ip, "10.0.0.196")


    def test_the_red_stage_passes_the_placement_to_the_resolver(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            comp = Path(td)
            placed = {"red01": {"ip": "10.0.0.241"}}
            seen = {}

            class Stop(Exception):
                pass

            def resolve(_args, _comp, placement):
                seen["placement"] = placement
                raise Stop

            args = SimpleNamespace(red_ip=None, red_vmid=1671, duration_min=25)
            with mock.patch.object(red_stage, "read_placement", return_value=placed), \
                    mock.patch.object(red_stage, "_resolve_red_vmid"), \
                    mock.patch.object(red_stage, "_resolve_red_ip", side_effect=resolve):
                with self.assertRaises(Stop):
                    red_stage.stage_red(args, comp, {}, Path(td) / "run")
            self.assertIs(seen["placement"], placed)


if __name__ == "__main__":
    unittest.main()


class GeneratedHelpers(unittest.TestCase):
    def test_scrim_envs_credlist_is_a_valid_bash_array(self):
        # `;`-separated entries inside `( )` are a bash syntax error — scrim.env must source
        import os
        import subprocess as sp
        import tempfile
        from scrim import blue_agent
        line = "CREDLIST=" + blue_agent.credlist_literal(
            {"airship": "pw-1", "blueteam": "pw 2';x"})
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / "scrim.env"
            env.write_text("set -a\n" + line + "\n")
            proc = sp.run(["bash", "-c", f'. "{env}"; echo "${{#CREDLIST[@]}}"'],
                          capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "2")

