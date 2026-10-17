"""Windows execution idioms — ONE owner.

v1 shipped six copies of two idioms across the tactics layer and they drifted
(audited). The rules:

* The landing shell on the base-windows-server template is cmd.exe
  (DefaultShell), so a plain one-liner must be wrapped for powershell. The
  wrap is double-quoted because cmd.exe mangles POSIX quoting; therefore a
  wrapped line must never contain a double quote — an old invariant carried
  by docstring in every v1 tactic. Here it is ASSERTED at ship time.
* Multi-line or quote-heavy scripts ship as -EncodedCommand (base64,
  UTF-16LE) — immune to the cmd->powershell quoting war; it is the cleanest
  idiom v1 ever produced (the Windows beacon installer ran on it).

Never mix: if you can't say it in one double-quote-free line, use ps().
"""

import base64

_PS_PREFIX = "powershell -NoProfile -NonInteractive "


class WindowsQuoteError(AssertionError):
    pass


def assert_shippable(line):
    """The no-double-quote invariant, enforced in code."""
    if '"' in line:
        raise WindowsQuoteError(
            "Windows wrapped commands must never contain double quotes — "
            "the landing shell is cmd.exe and the wrap is double-quoted; "
            "use winexec.ps(script) (EncodedCommand) instead")
    if "\n" in line:
        raise WindowsQuoteError("multi-line script must go through ps(), "
                                "not the single-line wrap")


def wrap(line):
    """Legacy Command-wrap: a single double-quote-free PS line."""
    assert_shippable(line)
    return _PS_PREFIX + f'-Command "{line}"'


def ps(script):
    """EncodedCommand: any script, quotes included, one argument."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return _PS_PREFIX + f"-EncodedCommand {encoded}"


def ps_line(line):
    """Explicit alias for the legacy single-line style (prefer ps())."""
    return wrap(line)
