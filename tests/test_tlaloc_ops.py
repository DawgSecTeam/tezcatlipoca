"""tlaloc_ops — engine-side red integration (Compfile contract, staging, verdict).

No live range: every subprocess is injected, so these prove the control flow, the
per-competition config rendering, the command shapes and the degrade-and-continue
contract — the things that silently go wrong on a real deploy.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from red import tlaloc_ops as to  # noqa: E402


class Completed:
    def __init__(self, returncode=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = returncode, out, err


class FakeCtx:
    def __init__(self, comp_dir, ssh_key="key.pem", engine_vmid=1300):
        self.comp_dir = Path(comp_dir)
        self.ssh_key_abs = ssh_key
        self.engine_vmid = engine_vmid
        self.state = {}
        self.saved = 0

    def save_state(self):
        self.saved += 1


def compfile(tmp, extra="", red_yaml=None):
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    body = "name testcomp\nteams 2\n" + extra
    if red_yaml:
        body += f"red_yaml {red_yaml}\n"
        (tmp / red_yaml).write_text(red_yaml_content := red_yaml_text)
    (tmp / "Compfile").write_text(body)
    return tmp


red_yaml_text = """assume_breach: full
depth: 3
duration_min: 90
ladder:
  enabled: true
  tiers:
    - {name: observe, at_pct: 0.0, modes: [recon]}
    - {name: easy, at_pct: 0.2, modes: [stop]}
caps: {max_down: 4, min_standing: 1}
"""


class RedBlockTests(unittest.TestCase):
    def test_dotted_keys_and_legacy_knobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp, "assume_breach 1\nassume_breach_depth 2\n"
                                 "red.duration_min 60\nred.caps.max_down 3\n")
            block = to.red_block(comp)
            self.assertEqual(block["assume_breach"], "light")
            self.assertEqual(block["depth"], 2)
            self.assertEqual(block["duration_min"], 60)
            self.assertEqual(block["caps"]["max_down"], 3)

    def test_red_yaml_pointer_is_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp, red_yaml="red.yaml")
            block = to.red_block(comp)
            self.assertEqual(block["assume_breach"], "full")
            self.assertEqual(block["duration_min"], 90)
            self.assertEqual(block["ladder"]["tiers"][1]["at_pct"], 0.2)

    def test_compfile_keys_win_over_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp, "red.duration_min 30\n", red_yaml="red.yaml")
            self.assertEqual(to.red_block(comp)["duration_min"], 30)

    def test_engine_selection_defaults_to_v1(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp, "assume_breach 1\n")
            self.assertEqual(to.red_engine(comp), "badauto")
            comp2 = compfile(Path(tmp) / "b", "red.engine tlaloc\n")
            self.assertEqual(to.red_engine(comp2), "tlaloc")
            comp3 = compfile(Path(tmp) / "c", "red.engine something-else\n")
            self.assertEqual(to.red_engine(comp3), to.DEFAULT_ENGINE)

    def test_validation_flags_every_bad_field(self):
        problems = to.validate_red_block({
            "assume_breach": "sometimes", "depth": 7, "duration_min": 0,
            "ladder": {"tiers": [
                {"name": "easy", "at_pct": 0.5, "modes": ["stop"]},
                {"name": "observe", "at_pct": 0.1, "modes": ["recon"]},
                {"name": "hard", "modes": ["stop"]},
                {"name": "brutal", "at_pct": 0.9, "modes": ["nuke"]}]},
            "caps": {"min_standing": 0},
        })
        fields = {p["field"] for p in problems}
        self.assertIn("red.assume_breach", fields)
        self.assertIn("red.depth", fields)
        self.assertIn("red.duration_min", fields)
        self.assertIn("red.caps.min_standing", fields)
        self.assertTrue(any("sorted" in p["problem"] for p in problems))
        self.assertTrue(any("needs at_pct" in p["problem"] for p in problems))
        self.assertTrue(any("unknown mechanism" in p["problem"] for p in problems))

    def test_a_good_block_validates_clean(self):
        self.assertEqual(to.validate_red_block(to.red_block(
            compfile(tempfile.mkdtemp(), red_yaml="red.yaml"))), [])


class ConfigTests(unittest.TestCase):
    def test_config_carries_the_comp_and_red_block_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp, "red.engine tlaloc\nred.red_ip 10.0.0.198\n",
                            red_yaml="red.yaml")
            cfg = to.tlaloc_config(comp, comp_id="cde-2026", engine_ip="10.0.0.92",
                                   c2_url="https://10.0.0.210")
            self.assertEqual(cfg["run"]["comp"], "cde-2026")
            self.assertEqual(cfg["run"]["state_dir"], to.STATE_DIR)
            self.assertEqual(cfg["red"]["assume_breach"], "full")
            self.assertEqual(cfg["red"]["duration_min"], 90)
            # engine-only keys never leak into the agent's config
            self.assertNotIn("engine", cfg["red"])
            self.assertNotIn("red_ip", cfg["red"])
            self.assertEqual(cfg["sensors"]["quotient_url"], "http://10.0.0.92")
            self.assertTrue(cfg["c2"]["enabled"])
            self.assertEqual(cfg["c2"]["operation"], "cde-2026")

    def test_write_config_lands_next_to_the_competition(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp, red_yaml="red.yaml")
            path = to.write_config(comp, comp_id="x1")
            self.assertTrue(path.exists())
            import yaml
            self.assertEqual(yaml.safe_load(path.read_text())["run"]["comp"], "x1")


class CommandTests(unittest.TestCase):
    def test_seed_runs_from_the_install_dir(self):
        cmd = to.seed_command()
        self.assertTrue(cmd.startswith(f"cd {to.INSTALL_DIR}"))
        self.assertIn(f"--config {to.CONFIG_PATH}", cmd)
        self.assertIn("sudo -n", cmd)
        self.assertNotIn("--depth", cmd)      # the pushed config decides the depth

    def test_comp_prep_is_absolute_and_offline_of_the_range(self):
        cmd = to.comp_prep_command("some/comp")
        self.assertIn("c2-prep", cmd)
        self.assertEqual(cmd[cmd.index("--comp") + 1], str(Path("some/comp").resolve()))

    def test_destroy_targets_the_competition(self):
        cmd = to.destroy_command("some/comp")
        self.assertEqual(cmd[cmd.index("--competition") + 1],
                         str(Path("some/comp").resolve()))
        self.assertIn("--yes", cmd)

    def test_unit_commands_reference_the_unit(self):
        self.assertIn(to.UNIT, to.unit_stop_command())
        self.assertIn(to.UNIT, to.unit_install_command())

    def test_ssh_argv_is_strict_and_bounded(self):
        argv = to.ssh_argv("k.pem", "10.0.0.198", "true", timeout=12)
        self.assertIn("StrictHostKeyChecking=no", argv)
        self.assertIn("ConnectTimeout=12", argv)
        self.assertEqual(argv[-1], "true")


class PlantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.comp = compfile(self.tmp.name, "red.engine tlaloc\n", red_yaml="red.yaml")
        self.ctx = FakeCtx(self.comp)
        self.degradations = []

    def tearDown(self):
        self.tmp.cleanup()

    def test_refuses_an_invalid_red_block_before_touching_anything(self):
        bad = compfile(Path(self.tmp.name) / "bad", "red.assume_breach maybe\n")
        ctx = FakeCtx(bad)
        with mock.patch.object(to, "record_degradation",
                               side_effect=lambda *a, **k: self.degradations.append(a)):
            out = to.plant_assume_breach(ctx)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "invalid red block")
        self.assertTrue(self.degradations)

    def test_a_failed_provisioning_degrades_and_continues(self):
        with mock.patch.object(to, "record_degradation",
                               side_effect=lambda *a, **k: self.degradations.append(a)):
            out = to.plant_assume_breach(self.ctx, provision=Completed(3, "", "boom"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "provision failed")
        self.assertTrue(any(d[0] == "tlaloc_red_deploy" for d in self.degradations),
                        self.degradations)

    def test_a_failed_stage_degrades_and_names_the_reason(self):
        runner = lambda cmd, **kw: Completed(9, "", "no route to host")  # noqa: E731
        with mock.patch.object(to, "record_degradation",
                               side_effect=lambda *a, **k: self.degradations.append(a)):
            out = to.plant_assume_breach(self.ctx, provision=Completed(0),
                                         runner=runner)
        self.assertFalse(out["ok"])
        self.assertTrue(any(d[0] == "tlaloc_stage" for d in self.degradations),
                        self.degradations)

    def test_contract_missing_fields_are_reported_not_ignored(self):
        config = "run:\n  comp: x\nred:\n  assume_breach: full\n"
        pushed = mock.Mock(returncode=0, stdout=config)
        runner = lambda cmd, **kw: Completed(0, json.dumps(["run.comp", "red.depth"]))  # noqa: E731
        with mock.patch.object(to, "_ssh_run", return_value=pushed):
            out = to.verify_config_contract("k.pem", "10.0.0.198", runner=runner)
        self.assertFalse(out["ok"])
        self.assertEqual(out["missing"], ["red.depth"])


class VerdictTests(unittest.TestCase):
    def test_collect_evidence_copies_only_what_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "restore-table.json").write_text("{}")
            (state / "report.md").write_text("# report")
            evidence = Path(tmp) / "evidence"
            pulled = to.collect_evidence(state, evidence)
            self.assertEqual(set(pulled), {"restore-table.json", "report.md"})
            self.assertTrue((evidence / "report.md").exists())

    def test_verdict_reads_the_agents_own_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp)
            (evidence / "restore-table.json").write_text(json.dumps({
                "summary": {"impacts": 12, "blue_restores": 3, "footholds": 6},
                "entries": [{"effect": "takedown", "ip": "192.168.1.10"}],
                "commands": [{"command": "systemctl start nginx"}]}))
            (evidence / "report.md").write_text("# report")
            verdict = to.write_verdict(evidence)
            self.assertEqual(verdict["entries"], 1)
            self.assertEqual(verdict["commands"], 1)
            self.assertEqual(verdict["summary"]["impacts"], 12)
            self.assertEqual(verdict["engine"], "tlaloc")
            self.assertTrue(Path(verdict["path"]).exists())

    def test_verdict_without_artifacts_is_empty_not_wrong(self):
        with tempfile.TemporaryDirectory() as tmp:
            verdict = to.red_verdict(tmp)
            self.assertEqual(verdict["entries"], 0)
            self.assertIsNone(verdict["restore_table"])


class StatusTests(unittest.TestCase):
    def test_status_reports_engine_block_and_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = compfile(tmp, "red.engine tlaloc\n", red_yaml="red.yaml")
            out = to.status(comp)
            self.assertEqual(out["engine"], "tlaloc")
            self.assertEqual(out["problems"], [])
            self.assertEqual(out["block"]["assume_breach"], "full")


if __name__ == "__main__":
    unittest.main()
