"""Repo hygiene gates: no secret-bearing file is tracked, and no env var drifts.

Two independent incidents motivate this file, both recorded in docs/security-disclosures.md:

  1. 2026-09-29 — 21 tracked terraform artifacts carried real per-competition
     passwords. Closed by gitignore curation.
  2. 2026-10-01 — `.env.pre-cde-20260929` (a hand-named env backup) stayed tracked
     for weeks and reached the public remote because .gitignore listed ".env" and
     ".env.realm-backup*" by name and missed the variant. It carried a live Proxmox
     API token plus box and per-team passwords. Closed by the blanket `.env.*` rule
     AND by this test, so a future rule edit cannot silently reopen it.

The env-var half exists because the variable reference had drifted: four variables
the code reads (TF_VAR_engine_mgmt_ip, TF_VAR_engine_mgmt_gw, TF_VAR_scoring_vm_id,
TEZ_THIN_HEADROOM, TF_VAR_team_identifiers) were absent from .env.example. Anything
read but undocumented now fails here instead of surprising an operator mid-event.

Offline and fast: reads tracked paths via `git ls-files` and scans them as text.
"""

import re
import subprocess
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

# Every env name is one of three kinds (see the header of .env.example):
#   TF_VAR_*  Terraform input      TEZ_*  tezcatlipoca knob      NAKON_*/VULNDB_*  vendored
# PROXMOX_* is the TLS-pin namespace (PROXMOX_CA_BUNDLE / PROXMOX_TLS_FINGERPRINT),
# set by the operator and documented in .env.example.
_ENV_NAME_RE = re.compile(
    r"^(TF_VAR_[a-z0-9_]+|TEZ_[A-Z0-9_]+|NAKON_[A-Z0-9_]+|VULNDB_[A-Z0-9_]+|PROXMOX_[A-Z0-9_]+)$"
)

# Read by code but deliberately NOT user-facing config, with the reason. Keep this
# list short and justified — it is the escape hatch, not the dumping ground. Entries
# here are exempt from the naming standard (they are internal handoff keys) but are
# still checked for staleness by test_internal_allowlist_has_no_dead_entries.
INTERNAL_ENV = {
    # Internal handoff between run-agent-scrim.py and the shell/python it generates
    # into scrim.env. Never set by an operator by hand. (MY_PW is also written there
    # but is only ever consumed by the generated shell, never read by Python, so it
    # is deliberately absent here — the staleness check below would flag it.)
    "ENGINE_IP": "scrim.env handoff written by run-agent-scrim.py",
    "MY_TEAM": "scrim.env handoff written by run-agent-scrim.py",
    "JAR": "scrim.env handoff written by run-agent-scrim.py",
    "SCRIM_WEB01_PORT": "scrim harness test hook (fresh opencode port per blue cycle)",
}

# High-signal secret shapes. Deliberately narrow: a broad "long string" rule would
# match hashes and base64 test fixtures. Each pattern must never match a *real*
# secret without also matching an obvious placeholder.
_SECRET_PATTERNS = {
    "proxmox API token": re.compile(r"root@pam![A-Za-z0-9._-]+![A-Za-z0-9-]{16,}"),
    "PVEAPIToken with literal value": re.compile(
        r"PVEAPIToken=[A-Za-z0-9._-]+![A-Za-z0-9-]{16,}"
    ),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "Slack token": re.compile(r"\bxox[abpsr]-[A-Za-z0-9-]{10,}"),
    "assigned .env secret with a real value": re.compile(
        r"^(?:TF_VAR_[a-z0-9_]*?(?:password|token)|[A-Z_]*(?:PASSWORD|SECRET|TOKEN))="
        r"(?!CHANGEME|$)(?!#)(?:[\"']?)(?![<{])[A-Za-z0-9!@#$%^&*_+=-]{16,}\s*$",
        re.MULTILINE,
    ),
}
_PLACEHOLDERS = ("CHANGEME", "PLACEHOLDER", "XXX", "EXAMPLE", "<secret", "REDACTED")

_ENV_READ_RE = re.compile(
    r"""(?:os\.environ(?:\.get)?\s*(?:\[\s*|\(\s*)|getenv\s*\(\s*)['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""
)


def _tracked_files():
    out = subprocess.run(
        ["git", "ls-files"], cwd=_REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    return out


def _read(path):
    try:
        return (_REPO / path).read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError):
        return None


class NoTrackedSecrets(unittest.TestCase):
    def test_no_env_variant_is_tracked_except_the_template(self):
        """The 2026-10-01 leak: a hand-named .env backup reached the public remote."""
        offenders = [
            f for f in _tracked_files()
            if (Path(f).name == ".env" or Path(f).name.startswith(".env."))
            and Path(f).name != ".env.example"
        ]
        self.assertEqual(
            offenders, [],
            "secret-bearing env file(s) tracked — they hold the Proxmox API token and "
            "box/team passwords. Untrack and rotate. See docs/security-disclosures.md "
            "'Security disclosure history':\n  " + "\n  ".join(offenders),
        )

    def test_env_variants_are_gitignored(self):
        """Belt and braces: .gitignore must actually ignore the variants."""
        for probe in (".env", ".env.pre-old", ".env.realm-backup-20260101", ".env.local"):
            r = subprocess.run(
                ["git", "check-ignore", "-q", probe], cwd=_REPO, capture_output=True
            )
            self.assertEqual(r.returncode, 0, f"{probe} is not gitignored")
        r = subprocess.run(
            ["git", "check-ignore", "-q", ".env.example"], cwd=_REPO, capture_output=True
        )
        self.assertNotEqual(r.returncode, 0, ".env.example must stay tracked")

    def test_no_tracked_file_contains_a_secret(self):
        hits = []
        for f in _tracked_files():
            text = _read(f)
            if text is None:
                continue
            for name, pat in _SECRET_PATTERNS.items():
                for m in pat.finditer(text):
                    frag = m.group(0)
                    if any(p.lower() in frag.lower() for p in _PLACEHOLDERS):
                        continue
                    line = text[: m.start()].count("\n") + 1
                    hits.append(f"{f}:{line} [{name}]")
        self.assertEqual(
            hits, [],
            "tracked file(s) contain what looks like a live secret:\n  " + "\n  ".join(hits),
        )

    def test_no_private_key_material_is_tracked(self):
        """The repo ships proxmox.pub on purpose; the private half must never appear."""
        offenders = []
        for f in _tracked_files():
            name = Path(f).name
            if name in ("proxmox", "id_rsa", "id_ed25519") or name.endswith(".pem"):
                offenders.append(f)
        self.assertEqual(offenders, [], f"private key material tracked: {offenders}")


class EnvVarStandard(unittest.TestCase):
    def setUp(self):
        self.example = _read(".env.example") or ""
        self.declared = set(re.findall(r"^#?([A-Za-z_][A-Za-z0-9_]*)=", self.example, re.MULTILINE))
        self.used = {}
        for f in _tracked_files():
            # tests/ are excluded from the env-var standard: they read OS variables
            # (PATH) and are not a configuration surface an operator has to fill in.
            if not f.endswith(".py") or f.startswith("tests/"):
                continue
            text = _read(f)
            if text is None:
                continue
            for m in _ENV_READ_RE.finditer(text):
                self.used.setdefault(m.group(1), set()).add(f)

    def test_every_env_var_read_is_declared_or_allowlisted(self):
        """Drift guard: four real variables were undocumented before this test existed."""
        undeclared = {
            name: sorted(files)
            for name, files in self.used.items()
            if name not in self.declared and name not in INTERNAL_ENV
        }
        self.assertEqual(
            undeclared, {},
            "code reads env var(s) that are neither in .env.example nor in the "
            "INTERNAL_ENV allowlist. Declare them in .env.example (with a comment) or "
            "add them to INTERNAL_ENV in this file with a reason:\n  "
            + "\n  ".join(f"{n}  <- {', '.join(fs)}" for n, fs in sorted(undeclared.items())),
        )

    def test_env_names_follow_the_naming_standard(self):
        """New variables must be TF_VAR_* (terraform), TEZ_* (our knobs), or vendored.

        INTERNAL_ENV members are exempt: they are handoff keys between this repo's own
        processes, not operator-facing configuration.
        """
        bad = sorted(
            n for n in self.used
            if n not in INTERNAL_ENV and not _ENV_NAME_RE.match(n)
        )
        self.assertEqual(
            bad, [],
            "env var name(s) break the naming standard (TF_VAR_*/TEZ_*/NAKON_*/VULNDB_*/"
            "PROXMOX_*): " + ", ".join(bad),
        )

    def test_internal_allowlist_has_no_dead_entries(self):
        """A stale allowlist entry silently weakens the gate above."""
        dead = sorted(n for n in INTERNAL_ENV if n not in self.used)
        self.assertEqual(
            dead, [],
            "INTERNAL_ENV lists var(s) no longer read by any module — remove them so the "
            "gate stays tight: " + ", ".join(dead),
        )


if __name__ == "__main__":
    unittest.main()
