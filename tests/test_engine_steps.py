"""engine_cmd_ops: every remote step names itself when it fails, and secrets stay out of argv.

engine_cmd_ops owns the pipeline's longest remote operations — bootstrap's apt install and
`compose build` at 1800s, `compose up` at 600s, the template's `compose up` — and every
one of them funnels through `_run_engine_cmd`. Before this, a timeout or a non-zero exit
escaped as the raw CalledProcessError/TimeoutExpired, whose only content is an
`ssh ... <cmd>` argv of the same shape for every step: an operator had to read the whole
traceback to learn WHICH step died, and deploy's generic per-phase handler could not
tell them apart. Now the step is in the exception message and the original exception
rides along as __cause__.

Also pinned here: the base64 wrapping of the Quotient .env secrets. The postgres/redis
passwords are interpolated into the remote command that writes /opt/quotient/.env, so
they must reach the box without ever appearing as a literal in argv — otherwise a
traceback, `ps`, or an audit log leaks them. Offline: subprocess.run is stubbed and no
ssh is executed.
"""

import base64
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from engine_bootstrap_ops import push_quotient_env
from engine_cmd_ops import _run_engine_cmd

_CTX = {
    "ssh_key_path": "/tmp/w8-fake-key",
    "vm_username": "ubuntu",
    "scoring_engine_ip": "10.0.0.10",
}

# `echo '<blob>' | base64 -d` — the one shape every secret-bearing command uses.
_BLOB_RE = re.compile(r"echo '([A-Za-z0-9+/=]+)' \| base64 -d")


class StepNaming(unittest.TestCase):
    def test_failing_step_names_itself_and_chains_the_cause(self):
        err = subprocess.CalledProcessError(1, ["ssh", "engine", "compose up"], stderr="boom")
        with mock.patch.object(subprocess, "run", side_effect=err):
            with self.assertRaises(RuntimeError) as cm:
                _run_engine_cmd(_CTX, "cd /opt/quotient && sudo docker compose up -d",
                                timeout=600, step="compose up")
        self.assertIn("engine step 'compose up' failed", str(cm.exception))
        self.assertIs(cm.exception.__cause__, err)

    def test_timeout_names_its_step_too(self):
        err = subprocess.TimeoutExpired(["ssh", "engine", "compose build"], 1800)
        with mock.patch.object(subprocess, "run", side_effect=err):
            with self.assertRaises(RuntimeError) as cm:
                _run_engine_cmd(_CTX, "sudo docker compose build", timeout=1800,
                                step="compose build")
        self.assertIn("engine step 'compose build' failed", str(cm.exception))
        self.assertIn("1800", str(cm.exception))
        self.assertIs(cm.exception.__cause__, err)

    def test_successful_run_is_returned_unchanged(self):
        sentinel = subprocess.CompletedProcess(["ssh"], 0, stdout="ok", stderr="")
        with mock.patch.object(subprocess, "run", return_value=sentinel) as run:
            got = _run_engine_cmd(_CTX, "true", timeout=30, capture=True, step="noop")
        self.assertIs(got, sentinel)
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_check_false_nonzero_returncode_still_returns(self):
        # The check=False steps (apt-cacher-ng probe, growpart) must keep returning the
        # CompletedProcess; only TimeoutExpired can be labelled there.
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

        # ...but the box still receives both, so the wrapping did not regress into
        # "secure but broken".
        blobs = [m.group(1) for arg in argv for m in [_BLOB_RE.search(arg)] if m]
        self.assertEqual(len(blobs), 1, "expected exactly one base64-wrapped .env write")
        decoded = base64.b64decode(blobs[0]).decode()
        self.assertIn(f"POSTGRES_PASSWORD={self.PG}\n", decoded)
        self.assertIn(f"REDIS_PASSWORD={self.REDIS}\n", decoded)

    def test_failed_env_push_names_the_step_without_leaking_the_secret(self):
        def fake_run(argv, **kwargs):
            # Realistic failure: the traceback carries the real argv, which is exactly
            # the path a plaintext secret would leak through.
            raise subprocess.CalledProcessError(1, argv)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as cm:
                push_quotient_env(_CTX, self.PG, self.REDIS)

        msg = str(cm.exception)
        self.assertIn("engine step 'write Quotient .env' failed", msg)
        self.assertNotIn(self.PG, msg)
        self.assertNotIn(self.REDIS, msg)
        self.assertIsInstance(cm.exception.__cause__, subprocess.CalledProcessError)


if __name__ == "__main__":
    unittest.main()
