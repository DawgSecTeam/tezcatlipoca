"""The Compfile's packet_source is relative to the comp dir; the verifier runs
from the engine worktree, so a bare name resolved against the cwd and failed
(live: cde-2026 pre-T0 verify: "packet profile not found: packet.yaml")."""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from scrim import compworld


class PacketPath(unittest.TestCase):
    def test_the_packet_is_resolved_against_the_comp_dir(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "Compfile").write_text("name X\npacket_source packet.yaml\n")
        (d / "packet.yaml").write_text("name: X\n")
        flags = compworld._verify_flags(d)
        self.assertIn("--packet", flags)
        self.assertEqual(flags[flags.index("--packet") + 1], str(d / "packet.yaml"))


if __name__ == "__main__":
    unittest.main()
