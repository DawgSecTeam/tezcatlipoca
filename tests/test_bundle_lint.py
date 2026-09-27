"""Bundle var-lint false positives that blocked a valid deploy (live-found 2026-09-26):
self-optional ${VAR-}/${VAR:+}, [ -n "${VAR-}" ] guards, in-script assignment corrupted by
an apostrophe in a comment, and vars named only in full-line comments. A genuinely required
${VAR:?} must still be caught."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import nakon_ops


def _bundle(script, vars=None):
    d = Path(tempfile.mkdtemp())
    (d / "blobs").mkdir()
    (d / "blobs" / "s1").write_text(script)
    (d / "manifest.json").write_text(json.dumps({
        "plans": {"p1": {"platform": "linux",
                         "steps": [{"name": "t", "script_sha256": "s1", "vars": vars or {}}]}}}))
    return d


class BundleLint(unittest.TestCase):
    def _clean(self, script, vars=None):
        try:
            nakon_ops._lint_bundle_vars(_bundle(script, vars))
            return True
        except SystemExit:
            return False

    def test_self_optional_forms_ok(self):
        self.assertTrue(self._clean('if [ -n "${GROUPS_ADD-}" ]; then usermod -aG "$GROUPS_ADD" u; fi\n'))
        self.assertTrue(self._clean('printf "%s" "${PAYLOAD_BODY-}" > "${PAYLOAD_PATH-}"\n'))
        self.assertTrue(self._clean('echo "${EXTRA:+got $EXTRA}"\n'))

    def test_in_script_assignment_with_apostrophe_comment_ok(self):
        # the apostrophe in the comment must not swallow the DEST= line below it
        self.assertTrue(self._clean("# set the user's wallpaper\nD=/a\nF=b\nS=/c\nDEST=\"$D/$F\"\ncp \"$S\" \"$DEST\"\n"))

    def test_var_only_in_comment_ok(self):
        self.assertTrue(self._clean('# reads $MOTD via printf "%s" "$VAR"\nprintf "%s" "$MOTD"\n', vars={"MOTD": "x"}))

    def test_genuinely_required_var_still_caught(self):
        self.assertFalse(self._clean('foo="${RULE:?RULE is required}"\n'))
        self.assertFalse(self._clean('cp "$SRC" "$DEST"\n'))  # DEST never assigned/guarded


if __name__ == "__main__":
    unittest.main()
