"""Nakon config generation, bundle building, and deployment via scoring engine."""

import json
import math
import shlex
import subprocess
import sys
from pathlib import Path

from constants import (
    DISRUPTIVE_CONFIGS,
    NAKON_DIR,
    PER_MACHINE_NAKON_BUDGET,
    SLOW_SERVICES,
    WINDOWS_ADMIN_USER,
)


def _is_windows_template(template_name):
    return "win" in template_name.lower()


def os_to_platform(template):
    """Classify a free-text template name the way nakon does: 'windows' if it has 'win'."""
    return "windows" if "win" in template.lower() else "linux"


def _nakon_randomize(platform, services_budget, vulns_budget):
    """Pick services+vulns via `nakon randomize --json` (cwd=NAKON_DIR for catalog access)."""
    cmd = [
        sys.executable, "-m", "nakon", "randomize",
        "--platform", platform,
        "--services", str(services_budget),
        "--vulns", str(vulns_budget),
        "--exclude", *SLOW_SERVICES,
        "--source", "auto", "--json",
    ]
    result = subprocess.run(cmd, cwd=str(NAKON_DIR), capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError(
            "nakon randomize failed — the vulndb (MySQL + vulndb-ui) has to be reachable from "
            "this machine, or VULNDB_UI_URL set. Check vendor/nakon/.env."
        )
    selection = json.loads(result.stdout.strip().splitlines()[-1])
    return selection["services"], selection["vulns"]


def generate_nakon_config(teams, boxes, difficulty, comp_dir, box_password, box_username="ubuntu"):
    services_path = comp_dir / "box_services.json"
    vulns_path = comp_dir / "box_vulns.json"

    if services_path.exists() or vulns_path.exists():
        pinned = json.loads(services_path.read_text()) if services_path.exists() else {}
        pinned_vulns = json.loads(vulns_path.read_text()) if vulns_path.exists() else {}
        box_configs = {
            box["name"]: (pinned.get(box["name"], []), pinned_vulns.get(box["name"], []))
            for box in boxes
        }
        pinned_from = ", ".join(
            p.name for p in (services_path, vulns_path) if p.exists()
        )
        print(f"  Using pinned configurations from {pinned_from}")
        services_path.write_text(
            json.dumps({name: svcs for name, (svcs, _) in box_configs.items()}, indent=2)
        )
        vulns_path.write_text(
            json.dumps({name: vulns for name, (_, vulns) in box_configs.items()}, indent=2)
        )
    else:
        box_configs = {}
        for box in boxes:
            platform = os_to_platform(box["template"])
            services, vulns = _nakon_randomize(
                platform, max(math.ceil(difficulty / 3), 1), max(difficulty, 1)
            )
            box_configs[box["name"]] = (services, vulns)

        services_path.write_text(
            json.dumps({name: svcs for name, (svcs, _) in box_configs.items()}, indent=2)
        )
        (comp_dir / "box_vulns.json").write_text(
            json.dumps({name: vulns for name, (_, vulns) in box_configs.items()}, indent=2)
        )


    machines = []
    for i, (team, box) in enumerate(
        ((t, b) for t in teams.values() for b in boxes), start=1
    ):
        services, vulns = box_configs[box["name"]]
        configurations = services + vulns
        configurations.sort(
            key=lambda c: (c if isinstance(c, str) else c["name"]) in DISRUPTIVE_CONFIGS
        )
        windows = _is_windows_template(box["template"])
        machines.append({
            "id": i,
            "name": f"{box['name']}-team{team['identifier']}",
            "ip": f"192.168.{team['identifier']}.{box['last_octet']}",
            "os": box["template"],
            "user": WINDOWS_ADMIN_USER if windows else box_username,
            "password": box_password,
            "configurations": configurations,
        })

    config_path = comp_dir / "nakon-config.json"
    config_path.write_text(json.dumps({"machines": machines}, indent=2))
    return config_path


def build_nakon_bundle(config_path):
    """Build (or reuse) the content-addressed Nakon bundle for this competition."""
    result = subprocess.run(
        [sys.executable, "-m", "nakon", "build",
         "--config", str(Path(config_path).resolve()),
         "--out", "bundles",
         "--json"],
        cwd=str(NAKON_DIR), capture_output=True, text=True, timeout=900,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError(
            "nakon build failed — the vulndb (MySQL + vulndb-ui) has to be reachable from "
            "this machine. Check vendor/nakon/.env."
        )

    info = json.loads(result.stdout.strip().splitlines()[-1])
    state = "cached" if info["cached"] else "fresh"
    print(f"  Nakon bundle {info['bundle_id'][:12]} ({state}, {info['plans']} plan(s), "
          f"{info['machines']} machine(s))")
    return NAKON_DIR / info["path"]


def run_nakon(key, scoring_user, scoring_ip, bundle, config_path, only=None, timeout=2400,
              strict=True):
    """Push the bundle to the scoring engine and run `nakon deploy` there."""
    ssh_base = [
        "ssh", "-i", str(key),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"{scoring_user}@{scoring_ip}",
    ]

    subprocess.run(ssh_base + ["rm -rf /tmp/nakon && mkdir -p /tmp/nakon"],
                   check=True, timeout=60)

    subprocess.run(
        [
            "scp", "-i", str(key),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-r",
            str(NAKON_DIR / "nakon"),
            str(bundle),
            str(Path(config_path).resolve()),
            f"{scoring_user}@{scoring_ip}:/tmp/nakon/",
        ],
        check=True, timeout=600,
    )

    remote_config = f"/opt/nakon/{Path(config_path).name}"
    only_args = ""
    if only:
        only_args = " --only " + " ".join(shlex.quote(name) for name in only)
    strict_arg = " --strict" if strict else ""

    try:
        subprocess.run(
            ssh_base + [
                "sudo mkdir -p /opt/nakon && sudo rm -rf /opt/nakon/* && "
                "sudo cp -r /tmp/nakon/. /opt/nakon/ && "
                "sudo pip3 install --break-system-packages paramiko 2>/dev/null; "
                "cd /opt/nakon && sudo python3 -m nakon deploy "
                f"--bundle /opt/nakon/{bundle.name} --config {remote_config}{only_args}{strict_arg}"
            ],
            check=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(ssh_base + ["sudo pkill -9 -f 'nakon deploy' || true"],
                           timeout=30)
        except Exception:
            pass
        raise


def _run_single_nakon_config(machine, configurations, key, scoring_user, scoring_ip, comp_dir,
                              tag, timeout=1800, strict=True):
    """Deploy one machine with overridden configs in isolation (reboot-safe)."""
    tmp_machine = {**machine, "configurations": configurations}
    tmp_config_path = comp_dir / f".nakon-domain-{tag}.json"
    tmp_config_path.write_text(json.dumps({"machines": [tmp_machine]}, indent=2))
    bundle = build_nakon_bundle(tmp_config_path)
    run_nakon(key, scoring_user, scoring_ip, bundle, tmp_config_path,
              only=[machine["name"]], timeout=timeout, strict=strict)


def is_windows_template(template_name):
    return _is_windows_template(template_name)
