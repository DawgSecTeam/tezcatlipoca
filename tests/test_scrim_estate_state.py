"""The scrim package reads the competition's ONE state document.

v0.3.0 replaced `.deploy_state.json` with `estate-state.json`, and the scrim side kept
reading the old file directly: `blue_agent.stage_blues` raised FileNotFoundError on the
first estate range (live 2026-10-15, run-75347fca) and the scrim never reached T0.
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from scrim import core, red_stage  # noqa: E402

_LEGACY_READ = re.compile(r"read_comp_json\([^)]*\.deploy_state\.json"
                          r"|\.deploy_state\.json\"\)\.read_text")


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

    def test_a_pre_v03_range_still_reads_its_legacy_file(self):
        with tempfile.TemporaryDirectory() as td:
            comp = Path(td)
            (comp / ".deploy_state.json").write_text('{"run_id": "run-old"}')
            self.assertEqual(core.read_state(comp)["run_id"], "run-old")
            # read-only: rendering a legacy range never migrates it as a side effect
            self.assertFalse((comp / "estate-state.json").exists())

    def test_no_scrim_module_reads_the_legacy_file_directly(self):
        offenders = [f"{path.name}:{i}"
                     for path in Path(core.__file__).parent.glob("*.py")
                     for i, line in enumerate(path.read_text().splitlines(), 1)
                     if _LEGACY_READ.search(line)]
        self.assertEqual(offenders, [], "route these through core.read_state")


class RedIpResolution(unittest.TestCase):
    def test_the_scrim_carries_the_comps_red01_pin_into_the_rebuilt_config(self):
        """The scrim rebuilds bad-auto's config from scratch; a red_ip of None made bad-auto
        fall back to its own default and MOVED red01 off the address the deploy recorded
        (live 2026-10-15, run-a87e0d88: recorded 10.0.0.196, VM re-pointed to 10.0.0.198)."""
        with tempfile.TemporaryDirectory() as td:
            comp = Path(td)
            (comp / "Compfile").write_text("name c\nred01_ip 10.0.0.196\n")
            args = SimpleNamespace(red_ip=None)
            red_stage._resolve_red_ip(args, comp)
            self.assertEqual(args.red_ip, "10.0.0.196")

    def test_an_explicit_flag_still_wins(self):
        with tempfile.TemporaryDirectory() as td:
            comp = Path(td)
            (comp / "Compfile").write_text("name c\nred01_ip 10.0.0.196\n")
            args = SimpleNamespace(red_ip="10.0.0.240")
            red_stage._resolve_red_ip(args, comp)
            self.assertEqual(args.red_ip, "10.0.0.240")


if __name__ == "__main__":
    unittest.main()
