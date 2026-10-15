"""The engine's paramiko comes from the engine template, not from a deploy-time pip.

`run_nakon` used to `pip3 install --break-system-packages paramiko` whenever the engine
lacked it, which mutated the scoring engine's system python in the middle of a deploy and
raced concurrent runs on the engine's pip. `python3-paramiko` is now installed with the
other engine packages, so a deploy probes and refuses. These pin both halves: the bootstrap
really bakes it, and no command the deploy sends over ssh installs anything.
"""

import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from engine import engine_bootstrap_ops  # noqa: E402
from nakonops import nakon_run_ops  # noqa: E402


class _Stop(Exception):
    """Stands in for the deploy itself — the setup stage is what these tests read."""


class _Fake:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = None


def _drive(probe_rc):
# Run run_nakon with every subprocess call faked; returns each argv list it built.
    sent = []

    def fake_run(argv, **kwargs):
        sent.append(list(argv))
        if any("import paramiko" in str(a) for a in argv):
            return _Fake(probe_rc)
        return _Fake(0)

    def fake_popen(argv, **kwargs):
        sent.append(list(argv))
        raise _Stop()

    root = Path(tempfile.mkdtemp())
    bundle = root / "bundle"
    bundle.mkdir()
    config = root / "nakon-config.json"
    config.write_text("{}")

    with patch.object(nakon_run_ops.subprocess, "run", fake_run), \
            patch.object(nakon_run_ops.subprocess, "Popen", fake_popen):
        try:
            nakon_run_ops.run_nakon("key", "root", "10.0.0.252", bundle, config)
        except _Stop:
            pass
    return sent


class EngineTemplateBakesParamiko(unittest.TestCase):
    def test_bootstrap_installs_it_with_the_engine_packages(self):
        src = inspect.getsource(engine_bootstrap_ops.bootstrap_scoring_engine)
        self.assertIn("python3-paramiko", src)


class DeployOnlyProbes(unittest.TestCase):
    def test_nothing_is_installed_over_ssh(self):
        for argv in _drive(probe_rc=0):
            self.assertNotIn("pip", " ".join(argv))

    def test_probe_precedes_the_deploy(self):
        sent = " ".join(" ".join(a) for a in _drive(probe_rc=0))
        self.assertIn("import paramiko", sent)
        self.assertLess(sent.index("import paramiko"), sent.index("-m nakon deploy"))

    def test_a_pre_bake_engine_is_refused_by_name(self):
        with self.assertRaises(RuntimeError) as ctx:
            _drive(probe_rc=1)
        self.assertIn("paramiko", str(ctx.exception))
        self.assertIn("phase 2", str(ctx.exception))