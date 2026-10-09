"""realm_c2_ops — offline unit tests.

No live range and no network: subprocess is mocked, so these prove the knob
parsing, the derived bad-auto config, the systemd unit generation (every
transport + MCP), the provision script's guards, and the staging/provision
control flow.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import realm_c2_ops as rc  # noqa: E402


def _comp(d, extra=""):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    (d / "Compfile").write_text(f"name t\n{extra}")
    return d


class KnobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tz-realmc2-knobs-")

    def test_defaults(self):
        k = rc.compfile_knobs(_comp(self.tmp))
        self.assertEqual(k["repo"], "https://github.com/spellshift/realm.git")
        self.assertEqual(k["go_version"], "1.26.9")
        self.assertEqual(k["implant_host"], "10.0.0.117")
        self.assertTrue(k["enabled"])

    def test_overrides(self):
        k = rc.compfile_knobs(_comp(
            self.tmp,
            "realm_c2_local 0\nrealm_c2_repo https://example.com/realm.git\n"
            "realm_c2_go_version 1.27.0\nrealm_c2_implant_host 10.9.9.1\n"))
        self.assertFalse(k["enabled"])
        self.assertEqual(k["repo"], "https://example.com/realm.git")
        self.assertEqual(k["go_version"], "1.27.0")
        self.assertEqual(k["implant_host"], "10.9.9.1")


class LocalConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tz-realmc2-cfg-")
        self.bad_auto_cfg = rc.BAD_AUTO / "config.yaml"
        self.had_cfg = self.bad_auto_cfg.exists()
        self.orig = self.bad_auto_cfg.read_text() if self.had_cfg else None

    def tearDown(self):
        if self.had_cfg:
            self.bad_auto_cfg.write_text(self.orig)
        elif self.bad_auto_cfg.exists():
            self.bad_auto_cfg.unlink()

    def test_build_local_c2_config_retargets_at_red01(self):
        self.bad_auto_cfg.write_text(
            "realm:\n  enabled: true\n  c2_ip: 10.0.0.117\n  base_url: http://10.0.0.117:8000\n"
            "deploy:\n  red_ip: 10.0.0.199\n")
        cfg = rc.build_local_c2_config("10.0.0.198")
        self.assertEqual(cfg["realm"]["c2_ip"], "10.0.0.198")
        self.assertEqual(cfg["realm"]["base_url"], "http://10.0.0.198:8000")
        self.assertEqual(cfg["realm"]["verify_url"], "http://10.0.0.198:8000")
        self.assertEqual(cfg["deploy"]["red_ip"], "10.0.0.198")

    def test_realm_disabled_means_no_c2(self):
        self.bad_auto_cfg.write_text("realm:\n  enabled: false\n")
        self.assertIsNone(rc.build_local_c2_config("10.0.0.198"))

    def test_missing_config_returns_none(self):
        self.bad_auto_cfg.unlink()
        self.assertIsNone(rc.build_local_c2_config("10.0.0.198"))

    def test_prepare_local_c2_writes_derived_config_outside_bad_auto(self):
        self.bad_auto_cfg.write_text("realm:\n  enabled: true\n")
        path, realm = rc.prepare_local_c2(self.tmp, "10.0.0.198")
        self.assertEqual(path.parent, Path(self.tmp))
        self.assertEqual(path.name, ".realm-c2-config.yaml")
        self.assertEqual(realm["c2_ip"], "10.0.0.198")

    def test_engine_ip_comes_from_the_comps_credentials_file(self):
        """Same source bad-auto reads: the published scoreboard URL."""
        (Path(self.tmp) / "credentials.txt").write_text(
            "Competition: scrim-one\nScoreboard:  http://10.0.0.252\nadmin  pw\n")
        self.assertEqual(rc.engine_ip_for(self.tmp), "10.0.0.252")

    def test_engine_ip_falls_back_to_the_deploy_env(self):
        with mock.patch.dict(os.environ, {"TF_VAR_engine_mgmt_ip": "10.0.0.251"}):
            self.assertEqual(rc.engine_ip_for(self.tmp), "10.0.0.251")
        with mock.patch.dict(os.environ, {"TF_VAR_engine_mgmt_ip": ""}, clear=False):
            os.environ.pop("TF_VAR_engine_mgmt_ip", None)
            self.assertIsNone(rc.engine_ip_for(self.tmp))

    def test_derived_config_carries_the_engine_ip(self):
        """`badauto deploy` needs the engine address for the DNAT; the derived config
        must be self-sufficient (a relative --competition path broke its own lookup)."""
        import yaml
        self.bad_auto_cfg.write_text("realm:\n  enabled: true\n")
        (Path(self.tmp) / "credentials.txt").write_text("Scoreboard:  http://10.0.0.252\n")
        path, _ = rc.prepare_local_c2(self.tmp, "10.0.0.198")
        cfg = yaml.safe_load(path.read_text())
        self.assertEqual(cfg["deploy"]["engine_ip"], "10.0.0.252")
        self.assertEqual(cfg["deploy"]["red_ip"], "10.0.0.198")


class UnitGenerationTests(unittest.TestCase):
    def test_default_realm_gets_every_transport(self):
        units = dict(rc.redirector_units({}))
        self.assertEqual(set(units), {"tavern-http1-redirector.service",
                                      "tavern-dns-redirector.service",
                                      "tavern-quic-redirector.service",
                                      "tavern-icmp-redirector.service"})
        self.assertEqual(rc.transport_summary({}),
                         ["grpc", "http1", "dns", "icmp", "quic"])

    def test_transport_subset_respected(self):
        units = dict(rc.redirector_units({"transports": {"grpc": True, "http1": True}}))
        self.assertEqual(set(units), {"tavern-http1-redirector.service"})
        self.assertEqual(rc.transport_summary({"transports": {"grpc": True, "http1": True}}),
                         ["grpc", "http1"])

    def test_dns_domain_is_validated(self):
        with self.assertRaises(ValueError):
            rc.redirector_units({"transports": {"dns": {"domain": "bad domain;rm"}}})

    def test_tavern_unit_enables_mcp_and_durable_db(self):
        unit = rc.tavern_unit({"c2_port": 8000}, "/home/sysadmin", "pw")
        self.assertIn("ENABLE_AI_MCP=1", unit)
        self.assertIn("HTTP_LISTEN_ADDR=0.0.0.0:8000", unit)
        self.assertIn("MYSQL_ADDR=127.0.0.1:3306", unit)
        self.assertIn("SECRETS_FILE_PATH=/home/sysadmin/realm/secrets/tavern-secrets", unit)

    def test_provision_script_validates_inputs(self):
        with self.assertRaises(ValueError):
            rc.provision_script({}, repo="http://insecure.example/x.git")
        with self.assertRaises(ValueError):
            rc.provision_script({}, go_version="1.26")

    def test_provision_script_contains_units_and_health_checks(self):
        script = rc.provision_script({}, repo="https://github.com/spellshift/realm.git")
        self.assertIn("git clone --depth 1 https://github.com/spellshift/realm.git", script)
        self.assertIn("tavern.service", script)
        for u in ("tavern-http1-redirector.service", "tavern-dns-redirector.service",
                  "tavern-quic-redirector.service", "tavern-icmp-redirector.service"):
            self.assertIn(u, script)
        self.assertIn("go build -buildvcs=false -o tavern_updated ./tavern/", script)
        self.assertIn("setcap cap_net_raw+ep", script)          # icmp transport
        self.assertIn(":8001 ", script)                          # http1 listen check
        self.assertIn(":5300 ", script)                          # dns listen check
        self.assertIn(":8443 ", script)                          # quic listen check
        self.assertIn("mariadb", script.lower())                 # durable state pair
        self.assertIn("TimeoutStartSec=1800", script)            # first-init timeout lesson


class _CP:
    def __init__(self, returncode=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = returncode, out, err


class BashUnitTests(unittest.TestCase):
    """The unit-writing section is run in REAL bash: the quoted heredoc blocks
    expansion, so only the sed swap can put the live DB password in the unit."""

    @staticmethod
    def _section():
        """The generated unit-writing slice, made self-contained: the prologue
        (package installs, daemon-reload) is not part of a unit test."""
        script = rc.provision_script({"c2_port": 8000})
        lines = script.splitlines()
        start = next(i for i, l in enumerate(lines)
                     if l.startswith('say "writing systemd --user units"'))
        end = next(i for i, l in enumerate(lines) if "loginctl enable-linger" in l)
        prelude = (
            'set -euo pipefail\n'
            'say() { echo "[realm-c2] $*"; }\n'
            'REALM_DIR="$HOME/realm"\n'
            'BIN="$REALM_DIR/tavern_updated"\n'
            'UDIR="$HOME/.config/systemd/user"\n'
            ': "${DBPW:?DBPW env var must carry the tavern DB password}"\n'
        )
        return prelude + "\n".join(lines[start:end])

    def setUp(self):
        if not shutil.which("bash"):
            self.skipTest("bash not available")

    def test_units_land_with_the_live_password_not_the_placeholder(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory(prefix="tz-realmc2-bash-") as tmp:
            env = {**os.environ, "HOME": tmp, "DBPW": "deadbeefcafe1234"}
            run = subprocess.run(["bash", "-c", self._section()], env=env,
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            unit_dir = Path(tmp) / ".config" / "systemd" / "user"
            tavern = (unit_dir / "tavern.service").read_text()
            names = sorted(p.name for p in unit_dir.iterdir())
            mode = (unit_dir / "tavern.service").stat().st_mode & 0o777
        self.assertIn("MYSQL_PASSWD=deadbeefcafe1234", tavern)
        self.assertNotIn(rc.DBPW_PLACEHOLDER, tavern)
        self.assertNotIn("$DBPW", tavern)                      # never a literal
        self.assertIn("Environment=ENABLE_AI_MCP=1", tavern)
        self.assertEqual(mode, 0o600)                          # password-bearing file
        self.assertEqual(names, ["tavern-dns-redirector.service",
                                 "tavern-http1-redirector.service",
                                 "tavern-icmp-redirector.service",
                                 "tavern-quic-redirector.service",
                                 "tavern.service"])

    def test_script_refuses_to_run_without_the_db_password(self):
        """Guarded before any apt/clone: an empty DBPW must not create a DB user
        with an empty password."""
        import os
        import subprocess
        with tempfile.TemporaryDirectory(prefix="tz-realmc2-nodbpw-") as tmp:
            env = {k: v for k, v in os.environ.items() if k != "DBPW"}
            env["HOME"] = tmp
            run = subprocess.run(["bash", "-c", rc.provision_script({})],
                                 env=env, capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("DBPW", run.stderr + run.stdout)


class PubkeyTests(unittest.TestCase):
    """The server key every planted beacon must encrypt to (IMIX_SERVER_PUBKEY)."""

    JOURNAL = ('time=2026-10-09T02:39:15Z level=INFO msg="public key: '
               'grXG751R6OJm07dagC9m0iSlvWmSHGuGa+4XHRrY5E4="')
    PK = "grXG751R6OJm07dagC9m0iSlvWmSHGuGa+4XHRrY5E4="

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tz-realmc2-pk-")

    def test_pubkey_is_read_from_the_tavern_journal(self):
        with mock.patch.object(rc, "_ssh", return_value=_CP(0, out=self.JOURNAL)):
            self.assertEqual(rc.tavern_pubkey("10.0.0.198", "key"), self.PK)

    def test_missing_pubkey_is_none(self):
        with mock.patch.object(rc, "_ssh", return_value=_CP(0, out="nothing here")):
            self.assertIsNone(rc.tavern_pubkey("10.0.0.198", "key"))

    def test_push_sends_a_quoted_base64_payload(self):
        import base64
        with mock.patch.object(rc, "_ssh", return_value=_CP(0)) as ssh:
            rc.set_realm_pubkey("key", "10.0.0.198", self.PK)
        cmd = ssh.call_args[0][2]
        self.assertIn("base64 -d", cmd)
        decoded = base64.b64decode(cmd.split()[1]).decode()
        self.assertIn("realm.pubkey", decoded)
        self.assertIn(self.PK, decoded)

    def test_successful_provision_reports_and_pushes_the_pubkey(self):
        with mock.patch.object(rc, "_scp", return_value=_CP(0)), \
             mock.patch.object(rc, "_ssh", return_value=_CP(0, out=self.JOURNAL)) as ssh:
            out = rc.provision_realm_c2(self.tmp, "10.0.0.198", "key", {})
        self.assertTrue(out["ok"])
        self.assertEqual(out["pubkey"], self.PK)
        self.assertTrue(any("base64 -d" in str(c) for c in ssh.call_args_list))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tz-realmc2-exec-")

    def test_stage_implants_relays_both_implants(self):
        with mock.patch.object(rc, "_scp", return_value=_CP(0)) as scp, \
             mock.patch.object(rc, "_ssh", return_value=_CP(0)) as ssh:
            out = rc.stage_implants("key", "10.0.0.198", "10.0.0.117")
        self.assertTrue(out["ok"])
        self.assertEqual(scp.call_count, 3)          # pull linux, pull win, push both
        mv = ssh.call_args_list[-1][0][2]
        self.assertIn(rc.INSTALL_DIR, mv)

    def test_stage_implants_honours_the_configured_install_dir(self):
        """bad-auto's realm_plant reads install_dir from its staged config, so the
        relay must land the binaries in THAT dir or the plants refuse."""
        with mock.patch.object(rc, "_scp", return_value=_CP(0)), \
             mock.patch.object(rc, "_ssh", return_value=_CP(0)) as ssh:
            out = rc.stage_implants("key", "10.0.0.198", "10.0.0.117",
                                    {"install_dir": "/opt/custom-realm"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["install_dir"], "/opt/custom-realm")
        self.assertIn("/opt/custom-realm", ssh.call_args_list[-1][0][2])

    def test_stage_implants_failure_names_the_host(self):
        with mock.patch.object(rc, "_scp", return_value=_CP(1, err="nope")):
            out = rc.stage_implants("key", "10.0.0.198", "10.0.0.117")
        self.assertFalse(out["ok"])
        self.assertIn("10.0.0.117", out["error"])

    def test_provision_failure_carries_tail_and_ok_false(self):
        with mock.patch.object(rc, "_scp", return_value=_CP(0)), \
             mock.patch.object(rc, "_ssh", return_value=_CP(1, out="[realm-c2] build died")):
            out = rc.provision_realm_c2(self.tmp, "10.0.0.198", "key",
                                        {"c2_port": 8000})
        self.assertFalse(out["ok"])
        self.assertIn("rc=1", out["error"])
        self.assertIn("build died", out["error"])
        self.assertNotIn("mcp", out)

    def test_provision_success_reports_mcp_and_transports(self):
        with mock.patch.object(rc, "_scp", return_value=_CP(0)), \
             mock.patch.object(rc, "_ssh", return_value=_CP(0, out="[realm-c2] OK")):
            out = rc.provision_realm_c2(self.tmp, "10.0.0.198", "key",
                                        {"c2_port": 8000})
        self.assertTrue(out["ok"])
        self.assertEqual(out["mcp"], "/mcp")
        self.assertIn("grpc", out["transports"])
        self.assertEqual(out["red_ip"], "10.0.0.198")

    def test_provision_timeout_is_caught(self):
        """A hung ssh at ANY step (staging or the provision script) comes back
        as a summary, never as an exception — the module documents never-raise."""
        import subprocess as sp
        with mock.patch.object(rc, "_scp", return_value=_CP(0)), \
             mock.patch.object(rc, "_ssh",
                               side_effect=sp.TimeoutExpired(cmd="ssh", timeout=1)):
            out = rc.provision_realm_c2(self.tmp, "10.0.0.198", "key", {})
        self.assertFalse(out["ok"])
        self.assertIn("timed out", out["error"])
        self.assertFalse(out["implants"]["ok"])

    def test_staging_timeout_still_provisions_the_server(self):
        """Staging only relays prebuilt implants: if it times out the tavern is
        still built — the warning names the staging failure only."""
        import subprocess as sp

        def _ssh(ssh_key, target_ip, script, timeout=120, env_prefix=""):
            if script.startswith("mkdir -p /tmp/.realm-c2-stage"):
                raise sp.TimeoutExpired(cmd="ssh", timeout=timeout)
            return _CP(0, out="[realm-c2] OK")

        with mock.patch.object(rc, "_scp", return_value=_CP(0)), \
             mock.patch.object(rc, "_ssh", side_effect=_ssh):
            out = rc.provision_realm_c2(self.tmp, "10.0.0.198", "key", {})
        self.assertTrue(out["ok"])
        self.assertFalse(out["implants"]["ok"])
        self.assertIn("timed out", out["implants"]["error"])


if __name__ == "__main__":
    unittest.main()
