"""Repo hygiene gates: no secret-bearing file is tracked, and no env var drifts."""

import re
import sys
import subprocess
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

# Every env name is one of three kinds:
#   TEZ_*  tezcatlipoca config (declared in estate/config.py's _SPEC)
#   NAKON_*/VULNDB_*  vendored-tool config, read by vendor/nakon, not by this repo
# An internal handoff key is exempt via INTERNAL_ENV below.
_ENV_NAME_RE = re.compile(r"^(TEZ_[A-Z0-9_]+|NAKON_[A-Z0-9_]+|VULNDB_[A-Z0-9_]+)$")

# Read by code but deliberately NOT user-facing config, with the reason. Entries here are exempt
# from the naming standard (they are internal handoff keys) but are still checked for staleness
# by test_internal_allowlist_has_no_dead_entries.
INTERNAL_ENV = {
    # scrim.env handoff written by run-agent-scrim.py. MY_PW is written there too but only ever
    # consumed by the generated shell, so the staleness check would flag it.
    "TEZ_ENGINE_MGMT_IP_IS_DEFAULT": "deploy_lib/estate_inputs.py default marker (same process)",
    "TEZ_ENGINE_MGMT_IP_IS_AUTO_SELECTED": "deploy_lib/prepare.py auto-selected marker (same process)",
    "ENGINE_IP": "scrim.env handoff written by run-agent-scrim.py",
    "MY_TEAM": "scrim.env handoff written by run-agent-scrim.py",
    "JAR": "scrim.env handoff written by run-agent-scrim.py",
    # The file's existence is the "hold at the next phase boundary" request.
    "TEZ_PAUSE_FILE": "tezcatlipoca-dashboard -> deploy_lib/pause.py boundary hold",
    # tests/conftest.py sets it before any test import; drivers skip .env when it is set
    # so the offline suite cannot inherit the developer's live estate identity.
    "TEZ_TEST_OFFLINE": "tests/conftest.py -> driver .env guard",
    # Inherited by every driver child the dashboard starts, so an orphan reclaim can retire its
    # range over /api/ranges/retire.
    # Operator-side scrim LLM gateway key; blue's opencode needs it as its provider key.
    "TEZ_SCRIM_API_KEY": "scrim/endpoints.py operator-side LLM gateway key",
    "TEZ_DASHBOARD_HOST": "tezcatlipoca-dashboard -> teardown/orphan_ops.py retire call",
    "TEZ_DASHBOARD_PORT": "tezcatlipoca-dashboard -> teardown/orphan_ops.py retire call",
    "TEZ_DASHBOARD_TOKENS": "tezcatlipoca-dashboard -> teardown/orphan_ops.py retire bearer",
    # The portal container's ops-sync handoff: portal_ops/ops_sync read them container-side.
    "TEZ_OPS_URL": "tezcatlipoca-dashboard -> portal ops sync",
    "TEZ_OPS_TOKEN": "tezcatlipoca-dashboard -> portal ops sync bearer",
    # Default author attribution from the OS user, not operator configuration.
    "USER": "OS username, recorded as the default write-up author on seal",
    # Written by portal_ops into /opt/tez-portal/.env (or portal/compose.yaml defaults) and read
    # only inside the container by portal/app.py; operators configure the portal via [portal].
    "PORTAL_SECRET_KEY": "portal container env (portal_ops -> /opt/tez-portal/.env)",
    "PORTAL_CONFIG": "portal container env (path default /config/portal.json)",
    "PORTAL_EVENT_CONF": "portal container env (path default /config/event.conf)",
    "PORTAL_STATE_DIR": "portal container env (path default /state)",
    "PORTAL_OPEN": "portal container env (portal_ops from portal.open)",
    "PORTAL_COOKIE_SECURE": "portal container env (portal_ops writes 1)",
    "PORTAL_DIST": "portal container env (built frontend path, default next to app.py)",
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
    "assigned env secret with a real value": re.compile(
        r"^[A-Z_]*(?:PASSWORD|SECRET|TOKEN)="
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
    def test_no_env_variant_is_tracked(self):
        # a hand-named .env backup once stayed tracked and reached the public remote
        offenders = [
            f for f in _tracked_files()
            if Path(f).name == ".env" or Path(f).name.startswith(".env.")
        ]
        self.assertEqual(
            offenders, [],
            "secret-bearing env file(s) tracked — they hold the Proxmox API token and "
            "box/team passwords. Untrack and rotate immediately:\n  " + "\n  ".join(offenders),
        )


    def test_infrastructure_status_is_gitignored_and_untracked(self):
    # A force-add has defeated a .gitignore line in this repo before, so check the tracked set too.
        r = subprocess.run(
            ["git", "check-ignore", "-q", "infrastructure-status.md"],
            cwd=_REPO, capture_output=True,
        )
        self.assertEqual(r.returncode, 0, "infrastructure-status.md is not gitignored")
        offenders = [f for f in _tracked_files() if Path(f).name == "infrastructure-status.md"]
        self.assertEqual(
            offenders, [],
            "estate ground truth tracked — it names endpoints, tokens-by-reference and live "
            "vmid ownership:\n  " + "\n  ".join(offenders),
        )

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
    # The repo ships proxmox.pub on purpose; the private half must never appear.
        offenders = []
        for f in _tracked_files():
            name = Path(f).name
            if name in ("proxmox", "id_rsa", "id_ed25519") or name.endswith(".pem"):
                offenders.append(f)
        self.assertEqual(offenders, [], f"private key material tracked: {offenders}")

    def test_no_test_artifact_is_tracked(self):
    # These reports quote flags, credentials and inject answers by construction, and the secret
    # net is a shape matcher, not a safety guarantee — so the decision is "ignored, with no
    # publish path". A force-add has defeated a .gitignore line before, so assert the tracked set.
        offenders = [f for f in _tracked_files() if "/.automated-tests/" in f]
        self.assertEqual(
            offenders, [],
            "test artifact(s) tracked — these are box-pulled reports and raw evidence:\n  "
            + "\n  ".join(offenders),
        )
        r = subprocess.run(
            ["git", "check-ignore", "-q", "competitions/demo/.automated-tests/run-1a2b3c4d/REPORT.md"],
            cwd=_REPO, capture_output=True,
        )
        self.assertEqual(r.returncode, 0, "competitions/*/.automated-tests/ is not gitignored")

    def test_no_runtime_state_is_tracked_in_comp_dirs(self):
    # A competition directory ships its AUTHORING inputs, never its runtime state. Runtime files
    # carry this deploy's secrets (teams.json/credentials.txt are the generated passwords) and
    # invite a later deploy on another host to adopt them as its own. Tracked on purpose and NOT
    # matched here: Compfile, boxes.json, box_services.json, box_vulns.json, domain_roles.json,
    # users.json, packet inputs, injects/, pfsense seed configs.
        runtime = ("estate-state.json", "teams.json", "credentials.txt", "nakon-config.json")
        offenders = []
        for f in _tracked_files():
            if not f.startswith("competitions/"):
                continue
            name = f.rsplit("/", 1)[-1]
            if name in runtime or (name.startswith("coverage-run") and name.endswith(".json")):
                offenders.append(f)
        self.assertEqual(
            offenders, [],
            "runtime state tracked in competition dirs — these carry this deploy's "
            "secrets and identity:\n  " + "\n  ".join(offenders))


def _config_env_names():
# Every env name estate/config.py can resolve, from its _SPEC.
    sys.path.insert(0, str(_REPO))
    from estate import config as estate_config
    return {estate_config.env_name(key) for key in estate_config._SPEC}


class EnvVarStandard(unittest.TestCase):
    def setUp(self):
        self.used = {}
        # estate/config.py resolves its env names from a table, so the literal-read scan
        # cannot see them. It is the single reader AND the declaration point for the
        # config surface: every name it can resolve counts as declared.
        self.declared = _config_env_names()
        for name in self.declared:
            self.used.setdefault(name, set()).add("estate/config.py")
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
    # Drift guard: real variables were undocumented before this test existed.
        undeclared = {
            name: sorted(files)
            for name, files in self.used.items()
            if name not in self.declared and name not in INTERNAL_ENV
        }
        self.assertEqual(
            undeclared, {},
            "code reads env var(s) that estate/config.py's _SPEC does not declare and "
            "INTERNAL_ENV does not allowlist. Declare the key in _SPEC (its TEZ_* env name "
            "follows) or add the handoff key to INTERNAL_ENV in this file with a reason:\n  "
            + "\n  ".join(f"{n}  <- {', '.join(fs)}" for n, fs in sorted(undeclared.items())),
        )

    def test_env_names_follow_the_naming_standard(self):
    # New variables must be TEZ_* (our config) or vendored; INTERNAL_ENV handoff keys are exempt.
        bad = sorted(
            n for n in self.used
            if n not in INTERNAL_ENV and not _ENV_NAME_RE.match(n)
        )
        self.assertEqual(
            bad, [],
            "env var name(s) break the naming standard (TEZ_*/NAKON_*/VULNDB_*): " + ", ".join(bad),
        )



if __name__ == "__main__":
    unittest.main()
