"""Both shipped packets, end to end and offline: compile -> the deploy's input loaders and
secret mint -> credentials.txt -> the verifier's --packet gates -> nakon machine lists.

test_packet_compile.py covers the compiler in isolation and test_verify_gates.py the gates with
hand-made credentials. This stitches them with the REAL compiled bundle so a field the compiler
emits and a loader/gate stopped reading (or the reverse) shows up here."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import packet_ops  # noqa: E402
from deploy_lib import inputs as dl_inputs  # noqa: E402
from deploy_lib import secrets as dl_secrets  # noqa: E402
from deploy_lib import stages as dl_stages  # noqa: E402
from deploy_lib.phases import finish  # noqa: E402
from verifier import packet as vpacket  # noqa: E402

PACKETS = ("cde-2026", "maccdc-q-2026")


def _compile(root, name):
    path = _REPO / "packets" / name / "packet.yaml"
    profile = packet_ops.load_profile(path)
    comps = Path(root) / "competitions"
    comps.mkdir(exist_ok=True)
    comp_dir, _fidelity, _wrote = packet_ops.compile_profile(path, comps)
    return profile, Path(comp_dir)


class PacketPipelineOffline(unittest.TestCase):
    def _each(self):
        """[(name, tmp root)] per packet; the caller loops inside self.subTest."""
        out = []
        for name in PACKETS:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            out.append((name, tmp.name))
        return out

    def test_compile_outputs_are_loadable_by_the_deploy(self):
        for name, root in self._each():
          with self.subTest(packet=name):
            profile, comp = _compile(root, name)
            for rel in ("Compfile", "boxes.json", "box_services.json", "users.json",
                        "passwords.json", "packet-fidelity.md"):
                self.assertTrue((comp / rel).exists(), rel)
            for secret in ("passwords.json", "box_baseline.json", "domain_accounts.json"):
                if (comp / secret).exists():
                    self.assertEqual((comp / secret).stat().st_mode & 0o777, 0o600, secret)
            spec = dl_stages.CompetitionSpec()
            dl_inputs.load_competition_spec(spec, comp)
            self.assertEqual(spec.box_username, profile["credentials"]["box_username"])
            self.assertEqual([b["name"] for b in spec.boxes],
                             [b["name"] for b in profile["boxes"]])
            # every box template is a non-empty name; deploy preflight resolves it later
            self.assertTrue(all(b.get("template") for b in spec.boxes))

    def test_secret_mint_keeps_packet_credentials_and_verify_gate_passes(self):
        for name, root in self._each():
          with self.subTest(packet=name):
            profile, comp = _compile(root, name)
            spec = dl_stages.CompetitionSpec()
            dl_inputs.load_competition_spec(spec, comp)
            inputs = dl_stages.CompetitionInputs()
            dl_inputs.load_competition_inputs(inputs, comp)
            prior = dl_stages.PriorDeployState(state_path=comp / ".deploy_state.json",
                                               previous_state={}, resuming=False)
            secrets = dl_stages.CompetitionSecrets(
                state={"last_phase": 0}, teams={"team1": {"identifier": "101", "password": "tp"}},
                number_of_teams=1, run_id="run-0badf00d")
            identity = dl_stages.RunIdentity()
            identity.engine_vmid = 1000
            with patch("builtins.print"):
                dl_secrets.mint_competition_secrets(secrets, prior, spec, inputs, identity)
            creds = profile["credentials"]
            self.assertEqual(secrets.box_password, creds["box_password"])
            self.assertEqual(secrets.box_creds, creds["credlists"]["linux"])
            self.assertEqual(secrets.domain_creds, creds["credlists"].get("domain"))

            ctx = SimpleNamespace(
                name=spec.name, scoring_ip="10.0.0.9", admin_password=secrets.admin_password,
                scoring_password=secrets.scoring_password, packet_pw=inputs.packet_pw,
                inject_password=secrets.inject_password, teams=secrets.teams,
                box_username=spec.box_username, box_password=secrets.box_password,
                box_creds=secrets.box_creds, domain_creds=secrets.domain_creds, comp_dir=comp)
            finish.write_credentials_file(ctx)
            self.assertEqual((comp / "credentials.txt").stat().st_mode & 0o777, 0o600)
            with patch("builtins.print"):
                self.assertTrue(vpacket.check_packet_creds(comp, profile))

            # a rotated password is exactly what the gate must catch
            text = (comp / "credentials.txt").read_text().splitlines()
            tampered = [l.rsplit(" ", 1)[0] + " rotated-x" if l.startswith("box-login") else l
                        for l in text]
            (comp / "credentials.txt").write_text("\n".join(tampered) + "\n")
            with patch("builtins.print"):
                self.assertFalse(vpacket.check_packet_creds(comp, profile))
            # a missing credlist line fails too
            dropped = [l for l in text if not l.startswith("box-credlist-")]
            (comp / "credentials.txt").write_text("\n".join(dropped) + "\n")
            with patch("builtins.print"):
                self.assertFalse(vpacket.check_packet_creds(comp, profile))

    def test_packet_accounts_gate_against_real_profile(self):
        from verifier.model import Status
        profile = packet_ops.load_profile(_REPO / "packets/cde-2026/packet.yaml")
        users = profile["credentials"]["out_of_scope"]
        self.assertTrue(users)
        boxes = [{"name": "web01-t1", "ip": "192.168.101.4", "os": "ubuntu"}]
        ok = SimpleNamespace(returncode=0, stdout=" ".join(f"{u}=1" for u in users), stderr="")
        bad = SimpleNamespace(returncode=0, stdout=" ".join(f"{u}=0" for u in users), stderr="")
        with patch("builtins.print"):
            with patch("verifier.context.ssh_via_gateway", return_value=ok):
                self.assertEqual(vpacket.check_packet_accounts({}, profile, boxes).status,
                                 Status.PASS)
            with patch("verifier.context.ssh_via_gateway", return_value=bad):
                self.assertEqual(vpacket.check_packet_accounts({}, profile, boxes).status,
                                 Status.FAIL)
            # windows-only lineup: unprovable -> SKIP, never a pass
            win = [{"name": "ad01", "ip": "192.168.101.2", "os": "windows-server"}]
            self.assertEqual(vpacket.check_packet_accounts({}, profile, win).status,
                             Status.SKIP_UNAVAILABLE)

    def test_machine_lists_and_event_conf_from_compiled_bundle(self):
        import nakon_ops
        from quotient.setup import build_event_conf, expected_service_names
        for name, root in self._each():
          with self.subTest(packet=name):
            profile, comp = _compile(root, name)
            spec = dl_stages.CompetitionSpec()
            dl_inputs.load_competition_spec(spec, comp)
            teams = {"team1": {"identifier": "101", "password": "tp"},
                     "team2": {"identifier": "102", "password": "tp"}}
            cwd = os.getcwd()
            try:
                os.chdir(root)
                cfg = nakon_ops.generate_nakon_config(
                    teams, spec.boxes, spec.difficulty, comp, "bp", box_username=spec.box_username)
            finally:
                os.chdir(cwd)
            machines = json.loads(Path(cfg).read_text())["machines"]
            self.assertTrue(machines)
            # score-only pins are the engine's business, never nakon's
            blob = json.dumps(machines)
            self.assertNotIn("score/", blob)
            box_services = json.loads((comp / "box_services.json").read_text())
            names = expected_service_names(box_services, spec.boxes)
            self.assertTrue(names)
            conf = build_event_conf(
                {"teams": {k: v["identifier"] for k, v in teams.items()},
                 "boxes_per_team": spec.boxes, "event_name": spec.name,
                 "team_passwords": {k: "tp" for k in teams},
                 "quotient_admin_password": "ap", "quotient_scoring_password": "sp",
                 "inject_password": None}, box_services)
            self.assertIn("RequiredSettings", conf)
            blob = json.dumps(conf)
            for service_name in names:
                self.assertIn(service_name.split("-", 1)[1].lower(), blob.lower())


if __name__ == "__main__":
    unittest.main()
