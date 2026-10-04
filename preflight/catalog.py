"""Gate: `nakon catalog check` over the competition's pins."""

import json
import subprocess
import sys

from constants import NAKON_DIR


def catalog_check_paths(comp_dir):
    """Paths for `nakon catalog check`, filtered when the bundle uses constructs the
    catalog can't know about. Score-only pins ({"score_only": true}) name no catalog
    config; box_baseline.json pins plant like vulns but live in their own file. When
    neither is present the bundle's real files pass straight through. Returns
    (services_path, vulns_path, cleanup_fn)."""
    services = json.loads((comp_dir / "box_services.json").read_text())
    vulns = json.loads((comp_dir / "box_vulns.json").read_text())
    baseline_path = comp_dir / "box_baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}

    def _is_score_only(c):
        return isinstance(c, dict) and c.get("score_only")

    filtered_services = {box: [p for p in pins if not _is_score_only(p)]
                         for box, pins in services.items()}
    merged_vulns = {box: list(v) + list(baseline.get(box, []))
                    for box, v in vulns.items()}
    if filtered_services == services and merged_vulns == vulns:
        return (comp_dir / "box_services.json", comp_dir / "box_vulns.json", None)

    tmp_services = comp_dir / ".catalog-check-services.json"
    tmp_vulns = comp_dir / ".catalog-check-vulns.json"
    tmp_services.write_text(json.dumps(filtered_services, indent=2))
    tmp_vulns.write_text(json.dumps(merged_vulns, indent=2))

    def cleanup():
        tmp_services.unlink(missing_ok=True)
        tmp_vulns.unlink(missing_ok=True)

    return tmp_services, tmp_vulns, cleanup


def catalog_gate(comp_dir):
    svc_path, vuln_path, cleanup = catalog_check_paths(comp_dir)
    catalog = subprocess.run(
        [sys.executable, "-m", "nakon", "catalog", "check",
         "--boxes-json", str((comp_dir / "boxes.json").resolve()),
         "--box-services", str(svc_path.resolve()),
         "--box-vulns", str(vuln_path.resolve())],
        cwd=str(NAKON_DIR), capture_output=True, text=True, timeout=600)
    if cleanup:
        cleanup()
    if catalog.returncode != 0:
        print(catalog.stdout)
        print(catalog.stderr)
        raise SystemExit(
            "  ERROR: nakon catalog check reported errors for this competition's pins — "
            "fix or trim box_vulns.json/box_services.json before deploying (details above).")
    print("  Preflight: nakon catalog check 0 errors")
