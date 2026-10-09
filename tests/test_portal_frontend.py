"""The student portal's frontend is the management UI's design, not a look-alike.

portal/frontend (React + Vite + Tailwind, like webui/frontend) carries verbatim copies of webui's
shared kit and theme rather than importing across the tree: the portal image is built on the
engine from portal/ alone. These checks make the copies a single design — change one side and
the suite says so until the other matches."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WEBUI = ROOT / "webui" / "frontend"
PORTAL = ROOT / "portal" / "frontend"


class SharedDesign(unittest.TestCase):
    def test_shared_components_are_verbatim_copies(self):
        for name in ("ui.jsx", "kit.jsx"):
            self.assertEqual((PORTAL / "src/components" / name).read_text(),
                             (WEBUI / "src/components" / name).read_text(),
                             f"portal/frontend/src/components/{name} drifted from webui's — "
                             "re-copy it (cp webui/frontend/src/components/{name} portal/...)")

    def test_theme_css_is_webuis_plus_portal_additions(self):
        webui = (WEBUI / "src/index.css").read_text()
        webui = webui.replace('@plugin "@tailwindcss/typography";\n', "")  # webui-only (prose)
        portal = (PORTAL / "src/index.css").read_text()
        self.assertTrue(portal.startswith(webui),
                        "portal/frontend/src/index.css must start with webui's index.css "
                        "verbatim (minus the typography plugin); portal-only rules go after it")

    def test_same_stack_versions(self):
        w = json.loads((WEBUI / "package.json").read_text())
        p = json.loads((PORTAL / "package.json").read_text())
        shared = ["react", "react-dom", "react-router-dom", "@fontsource-variable/inter",
                  "@fontsource-variable/fraunces", "@fontsource-variable/jetbrains-mono"]
        for dep in shared:
            self.assertEqual(p["dependencies"][dep], w["dependencies"][dep], dep)
        for dep in ("vite", "tailwindcss", "@tailwindcss/vite", "@vitejs/plugin-react"):
            self.assertEqual(p["devDependencies"][dep], w["devDependencies"][dep], dep)

    def test_novnc_is_pinned_to_a_release_tarball_with_integrity(self):
        lock = json.loads((PORTAL / "package-lock.json").read_text())
        entry = lock["packages"]["node_modules/@novnc/novnc"]
        self.assertTrue(entry["resolved"].endswith("/refs/tags/v1.6.0.tar.gz"), entry)
        self.assertTrue(entry["integrity"].startswith("sha512-"))


if __name__ == "__main__":
    unittest.main()
