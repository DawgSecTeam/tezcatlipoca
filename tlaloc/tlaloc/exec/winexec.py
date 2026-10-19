"""Windows execution idioms — ONE owner."""

import base64

_PS_PREFIX = "powershell -NoProfile -NonInteractive "


class WindowsQuoteError(AssertionError):
    pass


def assert_shippable(line):
    if '"' in line:
        raise WindowsQuoteError(
            "Windows wrapped commands must never contain double quotes — "
            "the landing shell is cmd.exe and the wrap is double-quoted; "
            "use winexec.ps(script) (EncodedCommand) instead")
    if "\n" in line:
        raise WindowsQuoteError("multi-line script must go through ps(), "
                                "not the single-line wrap")


def wrap(line):
    # DefaultShell on the base-windows-server template is cmd.exe: the wrap is
    # double-quoted, so a wrapped line must carry no double quote — ps() otherwise
    assert_shippable(line)
    return _PS_PREFIX + f'-Command "{line}"'


def ps(script):
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return _PS_PREFIX + f"-EncodedCommand {encoded}"


def ps_line(line):
    """Alias for the legacy single-line style; prefer ps()."""
    return wrap(line)
