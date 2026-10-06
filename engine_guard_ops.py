"""Round-loop watchdog install on the engine (Compfile round_loop_guard)."""

import base64
import json

from pathlib import Path
from engine_cmd_ops import _run_engine_cmd


def install_round_loop_guard(ctx, comp_dir, scoring_password):
    """Install the round-loop watchdog on the engine (opt-in, Compfile `round_loop_guard`).

    After an engine reboot Docker brings the containers back but the round loop stays
    stopped, and the scoreboard freezes while everything that reads it keeps working —
    an old UP is reported as a live UP (docs/known-issues.md). verify detects it and
    `--fix-round-loop` repairs it, but both need someone to run them.

    The timer authenticates as the dedicated `scoring` account, never `admin`: Quotient
    allows one session per account, so an unattended login on `admin` would evict the
    operator's, verify's or the harness's session on every tick. The decision itself is
    round_loop.round_loop_state — the same function verify's gate uses, pushed here so
    the two cannot drift.

    Idempotent: re-pushing the same files and re-enabling the timer is safe, so a resume
    or a redeploy with a fresh `scoring` password just rewrites them.
    """
    tools_dir = Path(__file__).resolve().parent / "tools"
    round_loop_src = (Path(__file__).resolve().parent / "round_loop.py").read_text()
    guard_src = (tools_dir / "round_loop_guard.py").read_text()
    config = json.dumps({"base_url": "http://localhost", "username": "scoring",
                         "password": scoring_password}, indent=2)

    service = (
        "[Unit]\n"
        "Description=Restart Quotient's scoring round loop if an engine reboot stopped it\n"
        "After=docker.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/bin/python3 /usr/local/sbin/round_loop_guard.py --quiet\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Check the scoring round loop periodically\n"
        "\n"
        "[Timer]\n"
        # Delay is 60s, so a 60s tick sees a stopped loop within one round of it becoming
        # stale; the 120s boot delay lets Quotient come up before the first check.
        "OnBootSec=120\n"
        "OnUnitActiveSec=60\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )

    def b64(text):
        return base64.b64encode(text.encode()).decode()

    _run_engine_cmd(ctx, (
        f"echo '{b64(round_loop_src)}' | base64 -d | sudo tee /usr/local/sbin/round_loop.py > /dev/null && "
        f"echo '{b64(guard_src)}' | base64 -d | sudo tee /usr/local/sbin/round_loop_guard.py > /dev/null && "
        "sudo chmod 644 /usr/local/sbin/round_loop.py && "
        "sudo chmod 755 /usr/local/sbin/round_loop_guard.py && "
        f"echo '{b64(config)}' | base64 -d | sudo tee /opt/quotient/round-loop-guard.json > /dev/null && "
        "sudo chmod 600 /opt/quotient/round-loop-guard.json && "
        f"echo '{b64(service)}' | base64 -d | sudo tee /etc/systemd/system/round-loop-guard.service > /dev/null && "
        f"echo '{b64(timer)}' | base64 -d | sudo tee /etc/systemd/system/round-loop-guard.timer > /dev/null && "
        "sudo systemctl daemon-reload && sudo systemctl enable --now round-loop-guard.timer"
    ), timeout=60, step="install round-loop-guard unit+timer")
    print("    round-loop-guard.timer enabled (restarts a stopped round loop; logs as `scoring`)")
