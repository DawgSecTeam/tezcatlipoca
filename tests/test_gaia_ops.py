"""gaia_ops — the Mythic install/configure step (engine side, no live C2).

Every subprocess is injected, so these prove the command shapes against Gaia's
real argparse, the credential handoff (admin password, agent password, API key),
the callback address the payloads will dial, and the degrade-and-continue
contract — the parts that go wrong silently on a real deploy.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from red import gaia_ops as g  # noqa: E402


class Completed:
    def __init__(self, returncode=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = returncode, out, err


def comp_dir(tmp, extra=""):
    path = Path(tmp)
    path.mkdir(parents=True, exist_ok=True)
    (path / "Compfile").write_text("name testcomp\nteams 2\n" + extra)
    (path / "teams.json").write_text('{"team-1": {"identifier": 2}}')
    return path


class Runner:
    """Records argv, answers from a script, and can write Gaia's handoffs."""

    def __init__(self, gaia, fail_on=None, creds="mythic_admin:s3cret-pw\n",
                 box_password=None, reject_passwords=()):
        self.gaia, self.fail_on, self.creds = Path(gaia), fail_on, creds
        self.box_password = box_password
        self.reject_passwords = set(reject_passwords)
        self.calls = []

    def __call__(self, argv, **kwargs):
        argv = [str(a) for a in argv]
        self.calls.append({"argv": argv, "kwargs": kwargs})
        # the C2 box's own .env lookup (the admin-password fallback)
        if argv[0] == "ssh":
            return Completed(0, f"{self.box_password}\n" if self.box_password else "")
        is_gaia = len(argv) > 2 and argv[1].endswith("gaia.py")
        sub = argv[2] if is_gaia else argv[0]
        # fail_on names a gaia SUBCOMMAND (not any argv containing the word: the
        # setup step installs pip requirements, which also says "install")
        if self.fail_on and is_gaia and argv[2] == self.fail_on:
            return Completed(1, "", f"boom {self.fail_on}")
        if sub == "install":
            (self.gaia / "mythic_admin_creds.txt").write_text(self.creds)
        if sub == "user":
            return Completed(0, "tlaloc:a-gent-pw\n")
        if sub == "auth":
            # Mythic answers 403 "Authentication Failed" for a password its database
            # does not hold (a stale handoff file after an install re-run).
            if (kwargs.get("input") or "").strip() in self.reject_passwords:
                return Completed(1, "", "403 Authentication Failed")
            (self.gaia / ".env").write_text(
                'MYTHIC_API_KEY="api-key-123"\nMYTHIC_LOGIN_SERVER_PORT=7443\n')
        return Completed(0, "ok")

    def has(self, needle):
        return any(needle in " ".join(c["argv"]) for c in self.calls)


class CommandShapeTest(unittest.TestCase):
    def test_install_command_matches_gaia_argparse(self):
        argv = g.install_argv("/tmp/gaia", "10.0.0.198", "sysadmin", key="k.pem")
        assert argv[1].endswith("gaia.py")
        assert argv[2:] == ["install", "-S", "10.0.0.198", "-u", "sysadmin",
                            "-P", "22", "-i", "k.pem", "--install-updates",
                            "--install-deps", "--install-mythic"]

    def test_auth_uses_the_prompt_flag_and_https(self):
        # No -k: Gaia maps it to ssl=False (http) and Mythic serves TLS on 7443; the
        # deploy refused a competition on a 400-HTML auth (live 2026-10-17).
        argv = g.auth_argv("/tmp/gaia", "10.0.0.198")
        assert argv[2:] == ["auth", "-S", "10.0.0.198", "-P", "7443",
                            "-u", "mythic_admin", "-p"]
        assert "-k" not in argv

    def test_user_and_operation_commands(self):
        assert g.user_argv("/g", ["tlaloc"])[2:] == ["user", "create", "-u",
                                                     "tlaloc", "--cred-stdout"]
        assert g.operation_argv("/g", "scrim-one")[2:] == ["operation", "create",
                                                           "-n", "scrim-one"]

    def test_clone_is_depth_one_at_the_pinned_ref(self):
        argv = g.clone_argv("/tmp/gaia2")
        assert argv[:6] == ["git", "clone", "--depth", "1", "--branch", "main"]
        assert argv[-2] == g.GAIA_REPO and argv[-1] == "/tmp/gaia2"


class HandoffTest(unittest.TestCase):
    def test_admin_password_and_env_key_parsing(self):
        assert g.parse_admin_password("mythic_admin:hunter2\n") == "hunter2"
        assert g.parse_admin_password("nonsense") is None
        assert g.parse_env_value('MYTHIC_API_KEY="abc"\n', "MYTHIC_API_KEY") == "abc"
        assert g.parse_env_value("OTHER=1\n", "MYTHIC_API_KEY") is None

    def test_env_file_body_carries_the_secret_and_nothing_else_does(self):
        body = g.env_file_body("pw-1", host="10.0.0.198")
        assert f"{g.PASSWORD_ENV}=pw-1" in body and "MYTHIC_HOST=10.0.0.198" in body
        block = g.tlaloc_c2_block("/tmp", server="10.0.0.198", operation="c",
                                  callback_host="http://172.31.2.1", callback_port=4470)
        assert "pw-1" not in repr(block)
        assert block["password_env"] == g.PASSWORD_ENV and block["enabled"] is True

    def test_ensure_checkout_clones_only_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gaia"
            path.mkdir()
            (path / "gaia.py").write_text("")
            runner = Runner(path)
            ok, detail = g.ensure_checkout(path, runner=runner)
            assert ok and detail == "present" and runner.calls == []
            missing = Path(tmp) / "gaia2"
            ok, detail = g.ensure_checkout(missing, runner=runner)
            assert ok is False and "no gaia.py" in detail      # the fake never clones
            assert runner.has("git clone")

    def test_setup_builds_the_venv_and_runs_on_it(self):
        """Gaia's deps are not in the engine's interpreter: it gets its own venv,
        and gaia.py runs on that interpreter once it exists."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gaia"
            path.mkdir()
            (path / "gaia.py").write_text("")
            (path / "requirements.txt").write_text("mythic==0.2.11\n")
            assert g.venv_python(path) != str(path / ".venv" / "bin" / "python")
            runner = Runner(path)
            ok, detail = g.ensure_setup(path, runner=runner)
            assert ok and detail == "created"
            assert runner.has("-m venv") and runner.has("requirements.txt")
            (path / ".venv" / "bin").mkdir(parents=True)
            (path / ".venv" / "bin" / "python").write_text("")
            assert g.venv_python(path) == str(path / ".venv" / "bin" / "python")
            argv = g.gaia_argv(path, "install", "-S", "10.0.0.198", "-u", "x")
            assert argv[0] == str(path / ".venv" / "bin" / "python")
            ok, detail = g.ensure_setup(path, runner=runner)
            assert ok and detail == "present"

    def test_a_failed_setup_is_a_degradation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gaia"
            path.mkdir()
            (path / "gaia.py").write_text("")
            (path / "requirements.txt").write_text("mythic==0.2.11\n")

            def runner(argv, **kw):
                if "pip" in argv[0]:
                    return Completed(1, "", "pip exploded")
                return Completed(0, "")

            ok, detail = g.ensure_setup(path, runner=runner)
            assert ok is False and "pip exploded" in detail

    def test_a_checkout_without_requirements_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gaia"
            path.mkdir()
            (path / "gaia.py").write_text("")
            ok, detail = g.ensure_setup(path, runner=Runner(path))
            assert ok is False and "requirements" in detail

    def test_a_utf16_requirements_file_is_normalised_for_pip(self):
        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "requirements.txt"
            req.write_bytes("mythic==0.2.11\n".encode("utf-16"))
            out = g.normalize_requirements(req)
            assert out != req and out.read_text(encoding="utf-8").startswith("mythic==")
            plain = Path(tmp) / "plain.txt"
            plain.write_text("mythic==0.2.11\n")
            assert g.normalize_requirements(plain) == plain


class CallbackAddressTest(unittest.TestCase):
    def test_port_comes_from_the_compfile_firewall_dnat(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = comp_dir(tmp, "firewall_dnat 4470->10.0.0.198\n")
            assert g.callback_port_for(comp) == 4470
            assert g.callback_port_for(comp_dir(tmp + "empty")) is None

    def test_box_defaults_to_red01_and_can_be_declared(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = comp_dir(tmp)
            assert g.c2_box_for(comp, default_ip="10.0.0.198") == "10.0.0.198"
            comp2 = comp_dir(tmp + "b", "red.c2_ip 10.0.0.240\n")
            assert g.c2_box_for(comp2, default_ip="10.0.0.198") == "10.0.0.240"

    def test_callback_address_reaches_the_agent_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = comp_dir(tmp, "firewall_dnat 4470->10.0.0.198\n")
            (comp / "boxes.json").write_text(
                '[{"name": "web01", "last_octet": 4, "template": "base-debian13-lite-fix"}]')
            host = g.callback_host_for(comp)
            port = g.callback_port_for(comp)
            assert host == "192.168.2.1" and port == 4470
            block = g.tlaloc_c2_block(comp, server="10.0.0.198", operation="comp",
                                      callback_host=host, callback_port=port)
            assert block["callback_host"] == "http://192.168.2.1"
            assert block["callback_port"] == 4470

    def test_an_in_path_firewall_moves_the_callback_to_the_transit_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = comp_dir(tmp, "firewall_dnat 4470->10.0.0.198\n")
            (comp / "boxes.json").write_text(
                '[{"name": "pfsense", "last_octet": 1, "in_path": true, "unmanaged": true}]')
            assert g.callback_host_for(comp) == "172.31.2.1"

    def test_callback_url_normalisation(self):
        assert g.callback_url("172.31.2.1") == "http://172.31.2.1"
        assert g.callback_url("https://172.31.2.1:9000") == "https://172.31.2.1"
        assert g.callback_url("") is None


class PrepareTest(unittest.TestCase):
    def _prepared(self, tmp, **kw):
        comp = comp_dir(tmp, "firewall_dnat 4470->10.0.0.198\n")
        gaia = Path(tmp) / "gaia"
        gaia.mkdir()
        (gaia / "gaia.py").write_text("")
        (gaia / "requirements.txt").write_text("mythic==0.2.11\n")
        runner = Runner(gaia, **kw)
        result = g.prepare_mythic(comp, box_ip="10.0.0.198", ssh_user="sysadmin",
                                  ssh_key="/keys/deploy", runner=runner, gaia=gaia,
                                  callback_host="http://172.31.2.1",
                                  callback_port=4470, log=None)
        return result, runner

    def test_happy_path_installs_configures_and_hands_the_agent_its_creds(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, runner = self._prepared(tmp)
            assert result["ok"] is True, result["degradations"]
            assert result["reached"] == ["install", "auth", "user", "operation"]
            assert result["api_key"] == "api-key-123"
            assert result["c2"]["base_url"] == "https://10.0.0.198:7443"
            assert result["c2"]["operation"] == Path(tmp).name
            # the agent's OWN password travels, not the admin's
            assert f"{g.PASSWORD_ENV}=a-gent-pw" in result["env"]
            assert "s3cret-pw" not in result["env"]

    def test_every_gaia_command_runs_with_the_checkout_as_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, runner = self._prepared(tmp)
            gaia = str(Path(tmp) / "gaia")
            gaia_calls = [c for c in runner.calls if c["argv"][1].endswith("gaia.py")]
            assert len(gaia_calls) >= 4
            for call in gaia_calls:
                assert call["kwargs"].get("cwd") == gaia, call["argv"]

    def test_the_admin_password_is_fed_to_auth_on_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, runner = self._prepared(tmp)
            auth = [c for c in runner.calls if "auth" in c["argv"]][0]
            assert auth["kwargs"].get("input") == "s3cret-pw\n"

    def test_a_failed_install_degrades_and_stops_before_configuring(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, runner = self._prepared(tmp, fail_on="install")
            assert result["ok"] is False
            assert result["reached"] == ["install"]
            assert result["degradations"] and "boom install" in result["degradations"][0]
            assert not runner.has("operation")
            assert result["c2"] is None and result["env"] is None

    def test_a_stale_creds_password_falls_back_to_the_boxs_own_env(self):
        """An install re-run leaves the handoff file and Mythic's database disagreeing.

        caught live: /auth answered 403 for the creds file's password while the
        box's own MYTHIC_ADMIN_PASSWORD returned 200 + an access_token — the stack was
        healthy and the step was blaming a C2 it could not log in to.
        """
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("time.sleep"):
                result, _ = self._prepared(tmp, creds="mythic_admin:stale-pw\n",
                                           box_password="good-pw",
                                           reject_passwords={"stale-pw"})
            creds_text = (Path(tmp) / "gaia" / "mythic_admin_creds.txt").read_text()
        assert result["ok"] is True, result["degradations"]
        assert result["admin_password"] == "good-pw"
        assert result["password_source"] == "the C2 box's /opt/Mythic/.env"
        assert "mythic_admin:good-pw" in creds_text, creds_text

    def test_no_password_that_authenticates_is_a_degradation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("time.sleep"):
                result, runner = self._prepared(tmp, creds="mythic_admin:only-pw\n",
                                                reject_passwords={"only-pw"})
        assert result["ok"] is False
        assert any("gaia auth" in d for d in result["degradations"]), result["degradations"]
        # the user/operation steps still ran — the chain degrades, it does not crash
        assert "user" in result["reached"] and "operation" in result["reached"]

    def test_install_timeout_is_generous(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, runner = self._prepared(tmp)
            install = [c for c in runner.calls
                       if len(c["argv"]) > 2 and c["argv"][1].endswith("gaia.py")
                       and c["argv"][2] == "install"][0]
            assert install["kwargs"]["timeout"] >= 3600

    def test_auth_runs_before_the_agent_user_is_created(self):
        """`auth` writes the API key the other subcommands authenticate with.

        Run `user create` first and Gaia prints its auth usage and exits non-zero —
        which is what the first live smoke reported as an argument error in
        `gaia user create`).
        """
        with tempfile.TemporaryDirectory() as tmp:
            _, runner = self._prepared(tmp)
            subs = [c["argv"][2] for c in runner.calls
                    if len(c["argv"]) > 2 and str(c["argv"][1]).endswith("gaia.py")]
            assert "auth" in subs and "user" in subs, subs
            assert subs.index("auth") < subs.index("user"), subs
            assert subs.index("auth") < subs.index("operation"), subs

    def test_a_failing_auth_is_retried_before_it_degrades(self):
        """Install can return while the web UI is up but not yet serving JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("time.sleep"):
                result, runner = self._prepared(tmp, fail_on="auth")
            auths = [c for c in runner.calls
                     if len(c["argv"]) > 2 and str(c["argv"][1]).endswith("gaia.py")
                     and c["argv"][2] == "auth"]
            assert len(auths) == g.AUTH_ATTEMPTS, len(auths)
            assert result["ok"] is False
            assert any("gaia auth" in d for d in result["degradations"])

    def test_no_box_is_a_degradation_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = comp_dir(tmp)
            result = g.prepare_mythic(comp, box_ip=None, ssh_user="sysadmin",
                                      ssh_key="/k", runner=Runner(tmp), log=None)
            assert result["ok"] is False and "no C2 box" in result["degradations"][0]


if __name__ == "__main__":
    unittest.main()
