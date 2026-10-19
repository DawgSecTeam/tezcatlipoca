"""engine_cmd_ops: every remote step names itself when it fails, and secrets stay out of argv."""

import base64
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from engine.engine_bootstrap_ops import (_QUOTIENT_INJECTS_PY, bootstrap_scoring_engine,
                                         push_quotient_env, _patch_quotient_injects_cmd)
from engine.engine_cmd_ops import _run_engine_cmd

_CTX = {
    "ssh_key_path": "/tmp/w8-fake-key",
    "vm_username": "ubuntu",
    "scoring_engine_ip": "10.0.0.10",
}

_BLOB_RE = re.compile(r"echo '([A-Za-z0-9+/=]+)' \| base64 -d")


class StepNaming(unittest.TestCase):



    def test_check_false_nonzero_returncode_still_returns(self):
        # check=False steps (apt-cacher-ng probe, growpart): only timeouts get labelled.
        sentinel = subprocess.CompletedProcess(["ssh"], 7, stdout="", stderr="")
        with mock.patch.object(subprocess, "run", return_value=sentinel):
            got = _run_engine_cmd(_CTX, "false", check=False, timeout=30, step="noop")
        self.assertEqual(got.returncode, 7)


class SecretHygiene(unittest.TestCase):
    PG = "PG-CLEARTEXT-W8"
    REDIS = "REDIS-CLEARTEXT-W8"

    def test_env_secrets_reach_the_box_base64_wrapped_never_literal_in_argv(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            push_quotient_env(_CTX, self.PG, self.REDIS)

        self.assertEqual(len(calls), 1)
        argv = calls[0]
        joined = " ".join(argv)
        self.assertNotIn(self.PG, joined, "postgres password leaked into argv")
        self.assertNotIn(self.REDIS, joined, "redis password leaked into argv")

        blobs = [m.group(1) for arg in argv for m in [_BLOB_RE.search(arg)] if m]
        self.assertEqual(len(blobs), 1, "expected exactly one base64-wrapped .env write")
        decoded = base64.b64decode(blobs[0]).decode()
        self.assertIn(f"POSTGRES_PASSWORD={self.PG}\n", decoded)
        self.assertIn(f"REDIS_PASSWORD={self.REDIS}\n", decoded)

    def test_failed_env_push_names_the_step_without_leaking_the_secret(self):
        def fake_run(argv, **kwargs):
            raise subprocess.CalledProcessError(1, argv)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as cm:
                push_quotient_env(_CTX, self.PG, self.REDIS)

        msg = str(cm.exception)
        self.assertIn("engine step 'write Quotient .env' failed", msg)
        self.assertNotIn(self.PG, msg)
        self.assertNotIn(self.REDIS, msg)
        self.assertIsInstance(cm.exception.__cause__, subprocess.CalledProcessError)


class QuotientInjectPatch(unittest.TestCase):
    """The embedded script is the artifact under test: run it exactly as the engine does."""

    STOCK = (
        '                from.className = ""\n'
        '                subject.className = ""\n'
        '                description.className = ""\n'
        '                \n'
        '                from.textContent = inject.Title\n'
        '                subject.textContent = inject.Title\n'
        '                                \n'
        '                let descTemp = document.createElement("div")\n'
        '\n'
        '                let descPara = document.createElement("p")\n'
        '                descPara.textContent = inject.Description\n'
        '                descTemp.appendChild(descPara)\n'
    )
    PATCHED = (
        '                from.className = ""\n'
        '                subject.className = ""\n'
        '                description.className = ""\n'
        '                \n'
        '                subject.textContent = inject.Title\n'
        '                                \n'
        '                let descTemp = document.createElement("div")\n'
        '\n'
        '                let descPara = document.createElement("p")\n'
        '                descPara.innerHTML = marked.parse(inject.Description)\n'
        '                descTemp.appendChild(descPara)\n'
    )

    def _run(self, text):
        path = Path(tempfile.mkdtemp()) / "injects.html"
        if text is not None:
            path.write_text(text, encoding="utf-8")
        return subprocess.run([sys.executable, "-c", _QUOTIENT_INJECTS_PY, str(path)],
                              capture_output=True, text=True), path

    def test_patch_applies_to_a_stock_template(self):
        r, path = self._run(self.STOCK)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), self.PATCHED)
        self.assertIn("patched", r.stdout)



    def test_missing_anchor_reports_failure(self):
        # Upstream rewrote the pane: a patch that does not apply must not be swallowed.
        r, path = self._run("<html>upstream rewrote the inject pane</html>")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("did not apply", r.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "<html>upstream rewrote the inject pane</html>")


    def test_bootstrap_runs_the_patch_after_the_clone(self):
        import engine.engine_bootstrap_ops as bo
        steps = []

        def fake_run_cmd(ctx, cmd, step, **kwargs):
            steps.append(step)
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        with mock.patch.object(bo, "_run_engine_cmd", side_effect=fake_run_cmd), \
                mock.patch.object(bo, "install_range_healthcheck"):
            bootstrap_scoring_engine(_CTX, "pg", "redis", quotient_ref="abc")
        self.assertIn("patch Quotient inject page", steps)
        self.assertLess(steps.index("clone Quotient"),
                        steps.index("patch Quotient inject page"))


if __name__ == "__main__":
    unittest.main()
