"""guest_file_read: the repo's only file-pull primitive, and its absent-vs-failed split.

Why this file exists: every other agent helper in range_ops runs a command and returns
`(exit_code, stdout, stderr)`, so an artifact could only be rebuilt by parsing stdout —
and a collection manifest has to record `absent` (a run that never wrote that log:
normal, no action) versus `failed` (the channel broke: an operator is needed). The one
thing this primitive must never do is collapse those two, so most of these tests pin
which exception a given channel failure produces, and that a file PVE already reported
missing is not re-probed through every channel.

Offline: proxmox_api and both exec helpers are stubbed. No env vars, no network. The one
test that shells out runs `base64 | tr` locally against a temp file (no guest), to prove
the quoting the Linux channel relies on survives a real `sh`/`bash`.
"""

import base64
import contextlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import range_ops  # noqa: E402

NODE = "proxmox"
VMID = 1234
PATH = "/var/log/tez/events.jsonl"

# What a live read of a file that is not there looks like on each channel.
PVE_NOT_FOUND = "unable to open file '/var/log/tez/events.jsonl': No such file or directory"
LINUX_NOT_FOUND = "base64: /var/log/tez/events.jsonl: No such file or directory"
WIN_NOT_FOUND = ('Exception calling "ReadAllBytes" with "1" argument(s): '
                 '"Could not find file \'C:\\evidence\\nope.jsonl\'."')
TRANSPORT = "got timeout"


class _Api:
    """Scripted proxmox_api for the file-read channel: records calls, then either
    returns a content body or raises what a live PVE would raise."""

    def __init__(self, content=None, error=None):
        self.calls = []
        self.content = content
        self.error = error

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.error is not None:
            raise self.error
        return {"data": {"content": self.content}}


class _Exec:
    """Recorder for one exec fallback channel; returns a scripted (rc, stdout, stderr)."""

    def __init__(self, result=(0, "", "")):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result

    @property
    def script(self):
        return self.calls[-1][0][2]

    @property
    def kwargs(self):
        return self.calls[-1][1]


@contextlib.contextmanager
def _channels(api, root=None, windows=None):
    """Patch all three channels, so a test can assert which ones were used."""
    root = _Exec() if root is None else root
    windows = _Exec() if windows is None else windows
    with patch.object(range_ops, "proxmox_api", api), \
         patch.object(range_ops, "guest_agent_exec_root", root), \
         patch.object(range_ops, "guest_agent_exec_windows", windows):
        yield root, windows


def _b64(data):
    return base64.b64encode(data).decode("ascii")


def _http_error(message):
    """A proxmoxer/requests-style failure, as proxmox_api surfaces it."""
    return RuntimeError(
        "500 Server Error: Internal Server Error for url: "
        f"https://pve:8006/api2/json/nodes/{NODE}/qemu/{VMID}/agent/file-read?file=... ({message})")


class PrimaryRoute(unittest.TestCase):
    """Channel 1: agent/file-read — used first, and its answer is trusted."""

    def test_pve_content_is_base64_decoded_to_exact_bytes(self):
        payload = b'{"event": "round-start"}\n\x00\xff\xfe binary tail\n'
        api = _Api(content=_b64(payload))
        with _channels(api) as (root, windows):
            self.assertEqual(range_ops.guest_file_read(NODE, VMID, PATH), payload)
        self.assertEqual(api.calls[0][0], "GET")
        self.assertTrue(api.calls[0][1].endswith(f"/nodes/{NODE}/qemu/{VMID}/agent/file-read"))
        self.assertEqual(api.calls[0][2]["params"], {"file": PATH})
        self.assertEqual(root.calls, [])       # no fallback when the fast channel works
        self.assertEqual(windows.calls, [])

    def test_already_decoded_text_body_is_taken_literally(self):
        # pmx.py:25,591 found this channel handing back decoded content; a body with a
        # space/newline is not base64 and must come through verbatim.
        api = _Api(content="hello world\nsecond line\n")
        with _channels(api):
            self.assertEqual(range_ops.guest_file_read(NODE, VMID, PATH),
                             b"hello world\nsecond line\n")

    def test_empty_file_is_empty_bytes_not_a_fallback(self):
        api = _Api(content="")
        with _channels(api) as (root, _):
            self.assertEqual(range_ops.guest_file_read(NODE, VMID, PATH), b"")
        self.assertEqual(root.calls, [])

    def test_not_found_error_is_absent_and_is_not_re_probed(self):
        api = _Api(error=_http_error(PVE_NOT_FOUND))
        with _channels(api) as (root, windows):
            with self.assertRaises(FileNotFoundError) as ctx:
                range_ops.guest_file_read(NODE, VMID, PATH)
        msg = str(ctx.exception)
        self.assertIn(PATH, msg)
        self.assertIn("does not exist", msg)
        self.assertEqual(len(api.calls), 1)    # asked once, got the answer
        self.assertEqual(root.calls, [])       # a missing file is not retried forever
        self.assertEqual(windows.calls, [])

    def test_other_error_falls_through_to_the_exec_channel_and_succeeds(self):
        payload = b"collected anyway\n"
        api = _Api(error=_http_error(TRANSPORT))
        root = _Exec((0, _b64(payload), ""))
        with _channels(api, root=root) as (_, windows):
            self.assertEqual(range_ops.guest_file_read(NODE, VMID, PATH), payload)
        self.assertEqual(len(root.calls), 1)
        self.assertEqual(windows.calls, [])


class LinuxExecFallback(unittest.TestCase):
    """Channel 2: `base64 <file> | tr -d '\\n'` through guest_agent_exec_root."""

    def test_rc_nonzero_with_no_such_file_is_absent(self):
        with _channels(_Api(error=_http_error(TRANSPORT)), root=_Exec((1, "", LINUX_NOT_FOUND))):
            with self.assertRaises(FileNotFoundError) as ctx:
                range_ops.guest_file_read(NODE, VMID, PATH)
        self.assertIn(PATH, str(ctx.exception))

    def test_rc_nonzero_with_other_stderr_is_failed_and_quotes_stderr(self):
        root = _Exec((1, "", "base64: invalid input"))
        with _channels(_Api(error=_http_error(TRANSPORT)), root=root):
            with self.assertRaises(RuntimeError) as ctx:
                range_ops.guest_file_read(NODE, VMID, PATH)
        msg = str(ctx.exception)
        self.assertIn("base64: invalid input", msg)   # the operator gets the guest's words
        self.assertIn(PATH, msg)

    def test_rc_zero_with_undecodable_output_is_failed(self):
        # "!!!" is the load-bearing case: lenient b64decode *discards* junk characters
        # and would return b"" here — a corrupt stream silently turned into an empty
        # file, the truncation-that-looks-complete failure this primitive must not have.
        for bad in ("not base64!!", "!!!", "aGVsbG8= extra"):
            root = _Exec((0, bad, ""))
            with _channels(_Api(error=_http_error(TRANSPORT)), root=root):
                with self.assertRaises(RuntimeError, msg=f"accepted {bad!r}") as ctx:
                    range_ops.guest_file_read(NODE, VMID, PATH)
            msg = str(ctx.exception)
            self.assertIn(PATH, msg)
            self.assertIn("not valid base64", msg)

    def test_rc_zero_with_empty_output_is_an_empty_file(self):
        root = _Exec((0, "", ""))
        with _channels(_Api(error=_http_error(TRANSPORT)), root=root):
            self.assertEqual(range_ops.guest_file_read(NODE, VMID, PATH), b"")

    def test_channel_error_on_both_routes_names_both_causes(self):
        def boom(*args, **kwargs):
            raise RuntimeError("guest-agent exec pid=7 on vmid 1234 did not finish within 60s")

        with _channels(_Api(error=_http_error(TRANSPORT)), root=boom):
            with self.assertRaises(RuntimeError) as ctx:
                range_ops.guest_file_read(NODE, VMID, PATH)
        msg = str(ctx.exception)
        self.assertIn(PATH, msg)
        self.assertIn("agent/file-read", msg)          # the first failure is not lost
        self.assertIn("did not finish within 60s", msg)

    def test_script_is_the_busybox_safe_form_with_a_quoted_path(self):
        path = "/var/log/it's a log/events.jsonl"
        root = _Exec((0, _b64(b"x"), ""))
        with _channels(_Api(error=_http_error(TRANSPORT)), root=root):
            range_ops.guest_file_read(NODE, VMID, path)
        self.assertEqual(root.script, f"base64 {shlex.quote(path)} | tr -d '\\n'")
        self.assertIn("'\"'\"'", root.script)   # shlex's escaped single quote
        self.assertNotIn("-w0", root.script)    # GNU-only: busybox would fail
        self.assertEqual(root.kwargs["shell"], "sh")   # POSIX pipeline; alpine has no bash


class WindowsExecFallback(unittest.TestCase):
    """Channel 3: [IO.File]::ReadAllBytes through guest_agent_exec_windows."""

    def test_windows_route_uses_powershell_and_decodes(self):
        payload = b"windows evidence\r\n"
        windows = _Exec((0, _b64(payload), ""))
        root = _Exec()
        win_path = "C:\\Program Files\\tez\\events.jsonl"
        with _channels(_Api(error=_http_error(TRANSPORT)), root=root, windows=windows):
            self.assertEqual(
                range_ops.guest_file_read(NODE, VMID, win_path, windows=True), payload)
        self.assertEqual(root.calls, [])       # the Linux channel is not used for Windows
        self.assertEqual(windows.script,
                         "[Convert]::ToBase64String([IO.File]::ReadAllBytes("
                         "'C:\\Program Files\\tez\\events.jsonl'))")

    def test_windows_powershell_could_not_find_is_absent(self):
        windows = _Exec((1, "", WIN_NOT_FOUND))
        with _channels(_Api(error=_http_error(TRANSPORT)), windows=windows):
            with self.assertRaises(FileNotFoundError) as ctx:
                range_ops.guest_file_read(NODE, VMID, "C:\\evidence\\nope.jsonl", windows=True)
        self.assertIn("C:\\evidence\\nope.jsonl", str(ctx.exception))

    def test_windows_rc_nonzero_with_other_stderr_is_failed(self):
        windows = _Exec((1, "", "Access to the path 'C:\\evidence\\log.jsonl' is denied."))
        with _channels(_Api(error=_http_error(TRANSPORT)), windows=windows):
            with self.assertRaises(RuntimeError) as ctx:
                range_ops.guest_file_read(NODE, VMID, "C:\\evidence\\log.jsonl", windows=True)
        self.assertIn("is denied", str(ctx.exception))

    def test_windows_path_with_a_single_quote_is_doubled(self):
        path = "C:\\evidence\\it's a log.jsonl"
        windows = _Exec((0, _b64(b"quoted"), ""))
        with _channels(_Api(error=_http_error(TRANSPORT)), windows=windows):
            self.assertEqual(range_ops.guest_file_read(NODE, VMID, path, windows=True),
                             b"quoted")
        self.assertIn("[IO.File]::ReadAllBytes('C:\\evidence\\it''s a log.jsonl')",
                      windows.script)
        # A bare (undoubled) quote would close the PowerShell literal early.
        self.assertNotIn("it's", windows.script)


class SizeGuard(unittest.TestCase):
    """Truncation that looks complete is worse than a loud failure."""

    def test_oversize_primary_result_names_size_limit_and_path(self):
        payload = b"x" * 100
        api = _Api(content=_b64(payload))
        with _channels(api):
            with self.assertRaises(RuntimeError) as ctx:
                range_ops.guest_file_read(NODE, VMID, PATH, max_bytes=32)
        msg = str(ctx.exception)
        self.assertIn("100 bytes", msg)      # what we got
        self.assertIn("max_bytes", msg)
        self.assertIn("32", msg)             # the limit
        self.assertIn(PATH, msg)
        self.assertIn("scp", msg)            # points at the channel that can carry it

    def test_oversize_fallback_result_is_guarded_too(self):
        root = _Exec((0, _b64(b"y" * 100), ""))
        with _channels(_Api(error=_http_error(TRANSPORT)), root=root):
            with self.assertRaises(RuntimeError) as ctx:
                range_ops.guest_file_read(NODE, VMID, PATH, max_bytes=64)
        self.assertIn("100", str(ctx.exception))
        self.assertIn("64", str(ctx.exception))

    def test_result_exactly_at_the_limit_is_allowed(self):
        payload = b"z" * 64
        with _channels(_Api(content=_b64(payload))):
            self.assertEqual(range_ops.guest_file_read(NODE, VMID, PATH, max_bytes=64), payload)


@unittest.skipUnless(shutil.which("sh") and shutil.which("bash") and shutil.which("base64"),
                     "needs sh, bash and base64 to prove both channels")
class LinuxCommandSurvivesRealShells(unittest.TestCase):
    """The Linux channel's script, run for real — the quoting is not just asserted."""

    def test_quoted_path_reads_the_right_file_under_sh_and_bash(self):
        contents = b"line one\nline two\x00\xff\n"
        with tempfile.TemporaryDirectory(prefix="tez guest dir ") as tmp:
            target = Path(tmp) / "it's ev'idence.jsonl"
            target.write_bytes(contents)

            root = _Exec((0, _b64(contents), ""))
            with _channels(_Api(error=_http_error(TRANSPORT)), root=root):
                self.assertEqual(range_ops.guest_file_read(NODE, VMID, str(target)), contents)
            script = root.script

            shells = [s for s in ("sh", "bash") if shutil.which(s)]
            for shell in shells:
                proc = subprocess.run([shell, "-c", script], capture_output=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(base64.b64decode(proc.stdout.strip()), contents,
                                 f"the {shell} channel mangled the file")
            self.assertEqual(shells, ["sh", "bash"])   # both, not just the one we ran on


class FailFastWithoutAnAnswer(unittest.TestCase):
    """A response with no content field falls through rather than returning junk."""

    def test_missing_content_field_falls_back(self):
        class _Empty:
            def __init__(self):
                self.calls = 0

            def __call__(self, method, path, **kwargs):
                self.calls += 1
                return {"data": {}}

        api = _Empty()
        root = _Exec((0, _b64(b"real bytes"), ""))
        with _channels(api, root=root):
            self.assertEqual(range_ops.guest_file_read(NODE, VMID, PATH), b"real bytes")
        self.assertEqual(api.calls, 1)
        self.assertEqual(len(root.calls), 1)


if __name__ == "__main__":
    unittest.main()
