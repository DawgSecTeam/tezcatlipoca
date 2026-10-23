"""The LLM endpoint table: a public API is never driven as an operator-side llama.cpp.

The string test (`"openrouter" not in base_url`) treated every non-cloud host — a keyed public
API included — as operator-side: blue's opencode.jsonc carried the literal string `local` as
its apiKey (401 on every cycle), red took the slow-local profile, red_tunnel opened a reverse
forward onto the endpoint's TLS port, a missing key degraded to `local`, and `check_red_llm`
required exactly HTTP 200 from an unauthenticated probe (401 on a keyed API) so the run aborted
pre-T0. These tests pin the table's classification instead.
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from net import jump_rules
from scrim import blue_helpers, core, endpoints, llm_endpoints, red_link, red_stage, red_tunnel

DEEPSEEK = "https://api.deepseek.com/v1"
LLAMA = "http://203.0.113.30:8080/v1"
OPENROUTER = "https://openrouter.ai/api/v1"


class Classification(unittest.TestCase):

    def test_public_api_is_not_operator_side(self):
        self.assertTrue(llm_endpoints.is_public(DEEPSEEK))
        self.assertFalse(core.is_local_endpoint(DEEPSEEK))



    def test_a_local_llama_url_stays_operator_side(self):
        self.assertTrue(core.is_local_endpoint(LLAMA))
        self.assertEqual(llm_endpoints.provider_name(LLAMA), "scrim-llm")
        self.assertEqual(llm_endpoints.key_env(LLAMA), "")



class ApiKey(unittest.TestCase):

    def test_public_endpoint_reads_its_own_env_var(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}, clear=False):
            self.assertEqual(endpoints.api_key(DEEPSEEK), "sk-test")

    def test_public_endpoint_without_a_key_raises_instead_of_inventing_one(self):
        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True), \
             tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory():
                with self.assertRaises(RuntimeError) as ctx:
                    endpoints.api_key(DEEPSEEK)
        self.assertIn("DEEPSEEK_API_KEY", str(ctx.exception))
        self.assertNotIn("local", str(ctx.exception))


    def test_blue_config_asks_opencode_for_the_deepseek_env_var(self):
        key_env = endpoints.key_env_name(DEEPSEEK)
        self.assertEqual("{env:%s}" % key_env, "{env:DEEPSEEK_API_KEY}")
        self.assertIn("{API_KEY_FIELD}", blue_helpers.OPENCODE_PROJECT_CFG)

    def test_an_operator_gateway_key_reaches_the_config_and_the_driver(self):
        with mock.patch.dict(os.environ, {"TEZ_SCRIM_API_KEY": "sk-gateway"}, clear=True):
            self.assertEqual(endpoints.api_key(LLAMA), "sk-gateway")
            self.assertEqual(endpoints.key_env_name(LLAMA), "TEZ_SCRIM_API_KEY")
            self.assertEqual(llm_endpoints.key_env(LLAMA), "TEZ_SCRIM_API_KEY")

    def test_an_operator_gateway_without_the_key_stays_local(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TEZ_SCRIM_API_KEY", "OPENROUTER_API_KEY")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(endpoints.api_key(LLAMA), "local")
            self.assertEqual(endpoints.key_env_name(LLAMA), "")



class RedProfileAndTunnel(unittest.TestCase):

    def _args(self, **kw):
        base = dict(llm_base_url=DEEPSEEK, red_model="deepseek-flash", reasoning_effort="",
                    red_ip="10.0.0.198", red_vmid=999, red_tunnel="auto")
        base.update(kw)
        return SimpleNamespace(**base)

    def test_public_endpoint_takes_the_cloud_profile(self):
        llm = red_stage.red_llm_config(self._args())
        self.assertEqual(llm["base_url"], DEEPSEEK)
        self.assertEqual(llm["model"], "deepseek-flash")
        self.assertTrue(llm["enabled"])
        self.assertEqual(llm["timeout_sec"], 240)
        self.assertEqual(llm["api_key_env"], "TLALOC_LLM_API_KEY")


    def test_auto_never_tunnels_to_a_public_endpoint(self):
        with mock.patch.object(red_tunnel.RedTunnel, "start") as start:
            self.assertIsNone(red_tunnel.maybe_start_red_tunnel(self._args()))
        start.assert_not_called()

    def test_explicit_on_is_refused_for_a_public_endpoint(self):
        with mock.patch.object(red_tunnel.RedTunnel, "start") as start:
            self.assertIsNone(red_tunnel.maybe_start_red_tunnel(self._args(red_tunnel="on")))
        start.assert_not_called()


class RedReachabilityGate(unittest.TestCase):

    def _probe(self, code):
        args = SimpleNamespace(competition="scrim-fixture", comp_dir=None, red_ip="10.0.0.198")
        with mock.patch.object(red_link, "_red_ssh_ctx",
                               lambda a: ("sysadmin@10.0.0.198", [], None)), \
             mock.patch.object(red_link.procs, "run_tree",
                               lambda *a, **k: SimpleNamespace(stdout=code, stderr="", returncode=0)):
            return red_link.check_red_llm(args, DEEPSEEK)

    def test_a_keyed_api_answering_401_counts_as_reachable(self):
        self.assertTrue(self._probe("401"))


    def test_no_answer_is_still_a_failure(self):
        self.assertFalse(self._probe("000"))


class RedSegmentEgress(unittest.TestCase):
    """red must be able to dial a public endpoint itself (no tunnel can carry TLS)."""

    RED = "10.200.0.0/24"

    def _rules(self):
        return jump_rules.jump_rules([120, 121], "10.0.0.248", red_segment=self.RED)

    def test_red_gets_a_wan_accept(self):
        rules = self._rules()
        self.assertIn(f"-A FORWARD -s {self.RED} ! -d 192.168.0.0/16 -j ACCEPT", rules)

    def test_red_gets_masqueraded_to_the_wan(self):
        rules = self._rules()
        self.assertIn(f"-A POSTROUTING -s {self.RED} ! -d 192.168.0.0/16 -j MASQUERADE", rules)

    def test_the_per_team_snat_still_precedes_the_wan_masquerade(self):
        rules = self._rules()
        team_snat = rules.index(f"-A POSTROUTING -s {self.RED} -d 192.168.120.0/24")
        wan = rules.index(f"-A POSTROUTING -s {self.RED} ! -d 192.168.0.0/16 -j MASQUERADE")
        self.assertLess(team_snat, wan)



class BlueNarrative(unittest.TestCase):

    def test_a_team_without_a_report_gets_a_synthesized_narrative(self):
        from scrim import blue_agent
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            wd = run_dir / "blue-team1"
            wd.mkdir()
            (wd / "feed.log").write_text("\n===== cycle T+12 rc=0 =====\nok\n"
                                         "\n===== cycle T+34 TIMEOUT =====\n")
            (wd / "LOG.md").write_text("# Blue team 1 — defense log\nhardened sshd\n")
            args = SimpleNamespace(run_dir=str(run_dir), teams=1, test_dir=str(run_dir))
            blue_agent.finalize_blue_narratives(args)
            body = (wd / "REPORT.md").read_text()
            self.assertIn("harness-synthesized", body)
            self.assertIn("cycle T+12 rc=0", body)
            self.assertIn("TIMEOUT", body)
            self.assertIn("hardened sshd", body)

    def test_a_real_report_is_never_overwritten(self):
        from scrim import blue_agent
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            wd = run_dir / "blue-team1"
            wd.mkdir()
            real = "# Blue team 1\n" + "judgement. " * 200
            (wd / "REPORT.md").write_text(real)
            args = SimpleNamespace(run_dir=str(run_dir), teams=1, test_dir=str(run_dir))
            blue_agent.finalize_blue_narratives(args)
            self.assertEqual((wd / "REPORT.md").read_text(), real)


if __name__ == "__main__":
    unittest.main()
