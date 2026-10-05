"""The explicit pipeline_api surface that replaced create-competition.py's importlib shim.

Regression class (audit 2026-10-02): redeploy-competition.py loaded create-competition.py
through importlib and reached library symbols as `driver.<name>`. Because the shim
re-exported everything it imported for its own use, redeploy's real dependency set was
invisible and the shim's surface could change under it silently. These tests pin the
replacement: one minimal surface, every name resolved from the module that owns it, and
no return of the loader."""

import ast
import importlib
import re
import subprocess
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import pipeline_api  # noqa: E402

# The contract, derived from the base commit by grepping `driver.` out of
# redeploy-competition.py: 25 call sites, 17 distinct names. owner module -> names.
_OWNERS = {
    "constants": ("PER_MACHINE_NAKON_BUDGET",),
    "config_ops": ("list_proxmox_templates",),
    "domain_ops": ("deploy_domain_configs",),
    "engine_ops": ("ensure_nat_forwarding",),
    "golden_ops": ("unbooted_golden_boxes",),
    "hardening_ops": ("fix_dns_on_boxes", "fix_services_on_boxes", "setup_ubuntu_auth"),
    "nakon_ops": ("build_nakon_bundle", "generate_nakon_config", "generate_stage_configs",
                  "os_to_platform", "run_nakon"),
    "ssh_ops": ("read_terraform_ctx", "wait_for_boxes_ssh", "wait_for_cloud_init"),
    "utils": ("env_summary",),
    "windows_ops": ("bootstrap_windows_box",),
}


class PipelineApiSurfaceTests(unittest.TestCase):
    def test_exports_exactly_the_consumer_set(self):
        expected = sorted(name for names in _OWNERS.values() for name in names)
        self.assertEqual(len(expected), 18)
        self.assertEqual(sorted(pipeline_api.__all__), expected)

    def test_every_export_resolves_to_its_owning_module(self):
        """No name may be routed through another re-export: the surface is the owner."""
        for module_name, names in _OWNERS.items():
            owner = importlib.import_module(module_name)
            for name in names:
                self.assertIs(
                    getattr(pipeline_api, name), getattr(owner, name),
                    f"pipeline_api.{name} must be {module_name}.{name}")

    def test_surface_matches_what_redeploy_actually_calls(self):
        """No over-export: the surface is exactly the set redeploy reaches for. A name
        added here but unused by any consumer is invisible again — the original bug."""
        source = (_REPO / "redeploy-competition.py").read_text()
        used = set(re.findall(r"pipeline_api\.([A-Za-z_][A-Za-z0-9_]*)", source))
        self.assertEqual(used, set(pipeline_api.__all__))


class ShimIsGoneTests(unittest.TestCase):
    def test_redeploy_has_no_driver_or_importlib_loader(self):
        source = (_REPO / "redeploy-competition.py").read_text()
        for gone in ("driver.", "_load_driver", "importlib", "sys.modules"):
            self.assertNotIn(gone, source, f"redeploy-competition.py still contains {gone!r}")

    def test_create_competition_is_a_thin_cli(self):
        tree = ast.parse((_REPO / "create-competition.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"pathlib", "dotenv", "deploy"})
        self.assertEqual(
            [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))], [],
            "create-competition.py must carry no library surface (see pipeline_api)")

    def test_create_competition_plan_only_still_runs(self):
        if not (_REPO / "competitions" / "example" / "boxes.json").exists():
            self.skipTest("competitions/example missing")
        out = subprocess.run(
            [sys.executable, "create-competition.py", "--competition", "example", "--plan-only"],
            capture_output=True, text=True, cwd=_REPO, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("PLAN for 'example'", out.stdout)


if __name__ == "__main__":
    unittest.main()
