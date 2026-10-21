"""sliver_ops — the engine-side install/configure step (no live C2)."""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from red import sliver_ops as s  # noqa: E402


class Completed:
    def __init__(self, returncode=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = returncode, out, err


def comp(tmp, body=""):
    path = Path(tmp)
    (path / "Compfile").write_text("name demo\n" + body)
    return path


def test_the_install_pins_the_release_and_runs_the_daemon_on_red01():
    script = s.install_script("10.0.0.198")
    assert s.SLIVER_VERSION == "v1.7.8"
    assert "sliver-server_linux-amd64" in script
    assert "--lhost 10.0.0.198 --lport 31337" in script
    assert "systemctl restart sliver.service" in script


def test_the_transport_map_is_every_v1_7_transport_and_keeps_dns_separate():
    ports = s.transport_ports()
    assert set(ports) == {"mtls", "http", "https", "dns", "wg"}
    assert ports["dns"]["gateway"] == 53 and ports["dns"]["server"] == 5353
    assert ports["wg"]["proto"] == "udp"


def test_sliver_is_the_default_and_mythic_is_explicit():
    with tempfile.TemporaryDirectory() as tmp:
        path = comp(tmp, "red.c2 off\n")
        assert s.sliver_enabled(path) is False
        (path / "Compfile").write_text("name demo\n")
        assert s.sliver_enabled(path) is True
        (path / "Compfile").write_text("name demo\nred.c2_engine mythic\n")
        assert s.sliver_enabled(path) is False


def test_the_plan_names_red01_and_a_per_competition_dns_domain():
    with tempfile.TemporaryDirectory() as tmp:
        path = comp(tmp, "red.c2_dns_domain ops.demo\n")
        plan = s.c2_plan(path, red_ip="10.0.0.198", comp_id="Sliver Demo")
        assert plan["box"] == "10.0.0.198"
        assert plan["domain"] == "ops.demo"
        assert plan["ports"]["mtls"]["server"] == 8888


def test_the_c2_block_carries_ports_and_the_profile_path_never_a_secret():
    block = s.sliver_c2_block("/tmp", server="10.0.0.198", operation="demo",
                              domain="c2.demo.range", ports=s.transport_ports())
    assert block["engine"] == "sliver" and block["enabled"] is True
    assert block["operator_config"] == s.OPERATOR_CONFIG
    assert "password" not in repr(block) and "token" not in repr(block)


def test_prepare_runs_install_operator_and_returns_the_block():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return Completed(0, "ok")

    with tempfile.TemporaryDirectory() as tmp:
        path = comp(tmp)
        result = s.prepare_sliver(path, box_ip="10.0.0.198", ssh_user="sysadmin",
                                  ssh_key="/k", runner=runner, log=None)
    assert result["ok"] is True, result
    assert result["c2"]["enabled"] is True
    assert result["reached"] == ["install", "operator"]
    assert all("sudo -n bash" in call[0][-1] for call in calls)


def test_a_failed_install_fails_the_step():
    def runner(argv, **kwargs):
        return Completed(1, "", "curl: (22)")

    with tempfile.TemporaryDirectory() as tmp:
        result = s.prepare_sliver(comp(tmp), box_ip="10.0.0.198",
                                  ssh_user="sysadmin", ssh_key="/k",
                                  runner=runner, log=None)
    assert result["ok"] is False
    assert any("curl" in d for d in result["degradations"])
