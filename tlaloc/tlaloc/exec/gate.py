"""The choke point: every action path passes through this gate.

The earlier agent's central lesson in one file: safety that is a convention is
safety that drifts (POSIX-only lint in front of cmd.exe, an unlinted FIFO RUN
path, a plant tactic that forgot ensure_target, a dead ensure_retries). tlaloc:

* ONE gate object, called by the shared effect execute() template — an effect
  cannot forget ensure_target, the kill switch, or the lint.
* danger_lint takes an OS and screens with the matching pattern set: the earlier
  agent's lint was POSIX-only while the same path fronted cmd.exe/powershell —
  del /f /s /q, format, Remove-Item -Recurse -Force, Stop-Computer, diskpart,
  reg delete, cipher /w all sailed through. The Windows set below closes
  exactly that audited list (deliberately blunt: a false positive just costs
  one candidate; a false negative is unrecoverable).
* Free-form command screening (length cap, one line) lives here too, so the
  cap is not re-typed per effect.
* clef never opens a gate: its command_intent classification informs
  attribution and ranking; hard safety stays deterministic.
"""

import ipaddress
import re

from ..errors import GuardrailError

# --- POSIX -------------------------------------------------------------------
# Screened for LLM-authored commands (improvise, task_channel). Combined-flag
# forms evade naive "-r ... -f" matching, so rm with ANY flags is banned.
DANGEROUS_POSIX = [
    (r"\brm\s+(-|--)[A-Za-z]", "rm with flags (recursive/force)"),
    (r"\bfind\b[^;|&]*-exec\b[^;|&]*\brm\b", "find -exec rm"),
    (r"\brm\s+[^;|&]*\s/\s*$", "rm targeting /"),
    (r"\bmkfs\b|\bwipefs\b|\bblkdiscard\b", "filesystem wipe"),
    (r"dd\s+[^;|&]*of=/dev/", "dd to raw device"),
    (r">\s*/dev/(sd|nvme|vd)", "redirect to raw device"),
    (r"\b(shutdown|reboot|halt|poweroff)\b|\binit\s+[06]\b", "power/reboot control"),
    (r":\(\)\s*\{.*\};\s*:", "fork bomb"),
    (r"chmod\s+-R\s+777\s+/(?!tmp)", "world-writable root tree"),
    (r"(curl|wget)[^;|&]*\|\s*(ba)?sh", "pipe-to-shell download"),
    (r"userdel\s+[^;|&]*-r", "user home deletion"),
    (r"\bpasswd\s+root\b|chpasswd[^;|&]*\broot\b|root:[^|;]*\|\s*(sudo\s+)?chpasswd",
     "root password change"),
]

# --- Windows -------------------------------------------------------------------
# The audited gap, closed: every pattern is one the earlier agent's POSIX lint
# let through in front of cmd.exe / powershell.
DANGEROUS_WINDOWS = [
    (r"\bdel\b[^\n]*\s/[a-z]*[fs]", "del /f or /s (forced/recursive delete)"),
    (r"\brd\s+[^\n]*/s\b", "rd /s recursive delete"),
    (r"\bRemove-Item\b[^\n]*-(Recurse|Force)", "Remove-Item -Recurse/-Force"),
    (r"\bformat(\.exe)?\s+[a-zA-Z]:", "format drive"),
    (r"\b(Stop|Restart)-Computer\b|\bshutdown\s*(/|\$|-)", "power control"),
    (r"\bdiskpart\b", "disk partitioning"),
    (r"\breg\s+delete\s+(HKLM|HKCU|HKU|HKCR|HKCC)", "registry hive delete"),
    (r"\bcipher\s+/w", "free-space wipe"),
    (r"\bvssadmin\s+delete", "shadow copy deletion"),
    (r"\\\\\.\\(PhysicalDrive|PHYSICALDRIVE)", "raw device write"),
    (r"\bStop-Process\b[^\n]*-Name\s+(winlogon|lsass|services|svchost|wininit)",
     "kill core Windows process"),
    (r"\bSet-Content\b[^\n]*C:\\Windows\\System32\\(config|drivers)",
     "overwrite system files"),
]


def danger_lint(command, os_name="linux"):
    """Return a rejection reason for destructive shell input, else None.
    Pattern-set choice by TARGET os — the audited defect was one shared POSIX set
    in front of both."""
    patterns = DANGEROUS_WINDOWS if str(os_name).lower() == "windows" else DANGEROUS_POSIX
    for pattern, why in patterns:
        if re.search(pattern, command, re.IGNORECASE if str(os_name).lower() == "windows" else 0):
            return why
    return None


class Gate:
    def __init__(self, cfg, team_subnets):
        self.kill_file = cfg["run"]["kill_file"]
        self.networks = [ipaddress.ip_network(n) for n in team_subnets]
        self.max_retries = cfg["limits"]["max_retries_per_service"]
        self.blue_nets = [ipaddress.ip_network(n if "/" in n else f"{n}/32")
                          for n in cfg["run"].get("blue_ips") or []]
        self.max_cmd_len = cfg["limits"]["command_max_len"]

    def check_kill_switch(self):
        try:
            with open(self.kill_file) as fh:
                reason = fh.read().strip() or "kill switch present"
            raise GuardrailError(f"kill switch active: {reason}")
        except FileNotFoundError:
            return

    def ensure_target(self, ip):
        """Team subnets (+ explicit run.blue_ips) are the only reachable
        targets. The engine, Proxmox, vulndb, C2 and the model endpoints are
        structurally unreachable as targets."""
        addr = ipaddress.ip_address(ip)
        if not any(addr in net for net in self.networks + self.blue_nets):
            raise GuardrailError(
                f"target {ip} is outside the team subnets "
                f"{[str(n) for n in self.networks]} — refusing")

    def ensure_retries(self, world, ip, key):
        """Cap consecutive failures per (ip, key) so we do not hammer a dead
        end. The earlier agent had this dead code; here the spray/reestablish
        effects call it."""
        tries = world.target(ip).get("creds_tried", {}).get(key, 0)
        if tries >= self.max_retries:
            raise GuardrailError(
                f"{key}@{ip} already failed {tries}x (cap {self.max_retries}) "
                "— pick another target")

    def ensure_command(self, command, os_name="linux"):
        """Full free-form screening: length, one-line, per-OS content lint."""
        if command is None or not str(command).strip():
            raise GuardrailError("empty command")
        cmd = str(command)
        if len(cmd) > self.max_cmd_len:
            raise GuardrailError(
                f"command rejected: {len(cmd)} chars exceeds cap "
                f"{self.max_cmd_len}")
        if "\n" in cmd.strip():
            raise GuardrailError("command rejected: free-form commands are one line")
        reason = danger_lint(cmd, os_name)
        if reason:
            raise GuardrailError(f"command rejected by danger lint ({os_name}): {reason}")
