"""Installing the round-loop watchdog on the engine (opt-in).

The timer is an unattended actor on a live engine, so two things must be pinned: it is
OFF unless the Compfile asks for it, and when it is on it gets the round_loop decision
module, the actor, a 0600 credentials file for the dedicated `scoring` account, and a
timer — and it authenticates as `scoring`, never `admin`.
"""

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import deploy_phases  # noqa: E402
import engine_ops  # noqa: E402


class _Recorder:
    """Fake _run_engine_cmd that records the shell command it was handed."""

    def __init__(self):
        self.calls = []

    def __call__(self, ctx, cmd, check=True, timeout=60, capture=False, step=None):
        self.calls.append({"cmd": cmd, "step": step})
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def joined(self):
        return "\n".join(c["cmd"] for c in self.calls)


class InstallPayload(unittest.TestCase):
    def setUp(self):
        self.rec = _Recorder()

    def _install(self, password="scoringpw"):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(engine_ops, "_run_engine_cmd", self.rec), \
                    patch("builtins.print"):
                engine_ops.install_round_loop_guard({"ssh_key_path": "/k"}, Path(d), password)

    def test_it_pushes_the_actor_the_decision_module_and_a_timer(self):
        self._install()
        blob = self.rec.joined()
        for expected in ("/usr/local/sbin/round_loop.py",
                         "/usr/local/sbin/round_loop_guard.py",
                         "/opt/quotient/round-loop-guard.json",
                         "/etc/systemd/system/round-loop-guard.service",
                         "/etc/systemd/system/round-loop-guard.timer",
                         "systemctl enable --now round-loop-guard.timer"):
            self.assertIn(expected, blob, f"missing {expected}")

    def test_the_credentials_file_is_0600_and_carries_the_scoring_account(self):
        self._install()
        blob = self.rec.joined()
        self.assertIn("chmod 600 /opt/quotient/round-loop-guard.json", blob)
        # The JSON is base64'd on the wire; decode the pushed payloads and find it.
        import base64
        import re
        found = None
        for match in re.finditer(r"echo '([A-Za-z0-9+/=]+)' \| base64 -d", blob):
            text = base64.b64decode(match.group(1)).decode()
            # The guard's own source mentions base_url, so only a payload that really
            # parses as the config counts.
            try:
                candidate = json.loads(text)
            except ValueError:
                continue
            if isinstance(candidate, dict) and "base_url" in candidate:
                found = candidate
        self.assertIsNotNone(found, "no config payload was pushed")
        self.assertEqual(found["username"], "scoring")   # never 'admin'
        self.assertEqual(found["password"], "scoringpw")
        self.assertEqual(found["base_url"], "http://localhost")

    def test_the_timer_ticks_at_most_once_a_minute(self):
        self._install()
        import base64
        import re
        blob = self.rec.joined()
        for match in re.finditer(r"echo '([A-Za-z0-9+/=]+)' \| base64 -d", blob):
            text = base64.b64decode(match.group(1)).decode()
            if "OnUnitActiveSec" in text:
                self.assertIn("OnUnitActiveSec=60", text)
                self.assertIn("OnBootSec=120", text)


class FlagGating(unittest.TestCase):
    """Off unless the Compfile asks. It is an unattended actor on a live engine."""

    def _ctx(self, comp_dir, flag):
        return types.SimpleNamespace(
            comp_dir=comp_dir, tf_ctx={"ssh_key_path": "/k"}, scoring_password="pw",
            from_phase=3, state={},
            teams={}, boxes=[], name="probe", inject_password=None,
            admin_password="a", postgres_password="p", redis_password="r",
            box_creds={}, domain_creds=None)

    def _run_phase3(self, comp_dir, flag):
        ctx = self._ctx(comp_dir, flag)
        ctx.repair_config_path = comp_dir / "repair.json"
        with patch.object(deploy_phases, "prepare_engine_from_template"), \
                patch.object(deploy_phases, "push_event_conf"), \
                patch.object(deploy_phases, "ensure_nat_forwarding"), \
                patch.object(deploy_phases, "timed",
                             side_effect=lambda *a, **k: contextlib.nullcontext()), \
                patch.object(deploy_phases, "install_round_loop_guard") as installer, \
                contextlib.redirect_stdout(io.StringIO()):
            deploy_phases.phase3_prepare_engine(ctx)
        return installer

    def test_no_flag_means_no_install(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            (comp_dir / "Compfile").write_text("name probe\n")
            installer = self._run_phase3(comp_dir, 0)
        installer.assert_not_called()

    def test_the_flag_installs_it(self):
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            (comp_dir / "Compfile").write_text("name probe\nround_loop_guard 1\n")
            installer = self._run_phase3(comp_dir, 1)
        installer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
