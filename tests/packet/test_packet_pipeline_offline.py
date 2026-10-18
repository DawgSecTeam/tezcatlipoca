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

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from config import packet_ops  # noqa: E402
from common.comp_paths import comps_root  # noqa: E402
from deploy_lib import inputs as dl_inputs  # noqa: E402
from deploy_lib import secrets as dl_secrets  # noqa: E402
from deploy_lib.context import DeployContext  # noqa: E402
from deploy_lib.phases import finish  # noqa: E402
from verifier import packet as vpacket  # noqa: E402

# The reference packets ship in the tezcatlipoca-competitions repo (beside their comp
# definitions); this pipeline test needs that checkout present.
PACKETS = ("cde-2026", "maccdc-q-2026")
_expect_packets = unittest.skipUnless(
    (comps_root() / PACKETS[0] / "packet.yaml").exists(),
    "packet.yaml lives in the tezcatlipoca-competitions repo")


def _compile(root, name):
    path = comps_root() / name / "packet.yaml"
    profile = packet_ops.load_profile(path)
    comps = Path(root) / "competitions"
    comps.mkdir(exist_ok=True)
    comp_dir, _fidelity, _wrote = packet_ops.compile_profile(path, comps)
    return profile, Path(comp_dir)


@_expect_packets
class PacketPipelineOffline(unittest.TestCase):
    def _each(self):
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
            dctx = DeployContext()
            dl_inputs.load_competition_spec(dctx, comp)
            self.assertEqual(dctx.box_username, profile["credentials"]["box_username"])
            self.assertEqual([b["name"] for b in dctx.boxes],
                             [b["name"] for b in profile["boxes"]])
            # every box template is a non-empty name; deploy preflight resolves it later
            self.assertTrue(all(b.get("template") for b in dctx.boxes))

    def test_secret_mint_keeps_packet_credentials_and_verify_gate_passes(self):
        for name, root in self._each():
          with self.subTest(packet=name):
            profile, comp = _compile(root, name)
            dctx = DeployContext(state_path=comp / "estate-state.json", previous_state={},
                                 resuming=False, state={"last_phase": 0},
                                 teams={"team1": {"identifier": "101", "password": "tp"}},
                                 number_of_teams=1, run_id="run-0badf00d", engine_vmid=1000)
            dl_inputs.load_competition_spec(dctx, comp)
            dl_inputs.load_competition_inputs(dctx, comp)
            with patch("builtins.print"):
                dl_secrets.mint_competition_secrets(dctx)
            creds = profile["credentials"]
            self.assertEqual(dctx.box_password, creds["box_password"])
            self.assertEqual(dctx.box_creds, creds["credlists"]["linux"])
            self.assertEqual(dctx.domain_creds, creds["credlists"].get("domain"))

            ctx = SimpleNamespace(
                name=dctx.name, scoring_ip="10.0.0.9", admin_password=dctx.admin_password,
                scoring_password=dctx.scoring_password, packet_pw=dctx.packet_pw,
                inject_password=dctx.inject_password, teams=dctx.teams,
                box_username=dctx.box_username, box_password=dctx.box_password,
                box_creds=dctx.box_creds, domain_creds=dctx.domain_creds, comp_dir=comp,
                state={})
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




if __name__ == "__main__":
    unittest.main()
