"""Build the nakon bundle on the operator host and lint its payload variable references."""

import json
import re
import subprocess
import sys
import threading

from constants import NAKON_DIR
from pathlib import Path


# M2.4: concurrent domain passes build their single-machine bundles on the operator host.
# The bundle dirs are content-addressed so parallel *different* configs don't collide, but
# the nakon build subprocess itself isn't audited for concurrent temp-state safety — one
# build at a time, deploys still run fully in parallel.
_BUNDLE_BUILD_LOCK = threading.Lock()

# Shell/env names a payload script may reference without any step declaring them.
# The bundle lint (below) fails on any OTHER uppercase reference the step's vars don't
# provide, so a catalog config that starts needing an undeclared var fails at bundle
# build instead of as a mid-plant rc=2 (hosts-redirect-linux / sudoers-rule, 2026-09-24).
_SHELL_VAR_WHITELIST = {
    "PATH", "HOME", "USER", "PWD", "OLDPWD", "SHELL", "TERM", "LANG", "SHLVL",
    "UID", "EUID", "PPID", "IFS", "PS1", "PS4", "HOSTNAME", "RANDOM", "SECONDS",
    "LINENO", "TMPDIR", "MAIL", "OPTARG", "OPTIND", "DEBIAN_FRONTEND",
    "SUDO_USER", "SUDO_UID", "SUDO_GID", "SUDO_COMMAND", "LOGNAME",
}
# Self-defaulting expansions are fine undeclared — strip them before scanning. ALL of
# ${VAR-x} ${VAR:-x} ${VAR=x} ${VAR:=x} ${VAR+x} ${VAR:+x} make the var optional (the
# script supplies/omits a value itself); the colon variants only differ on empty-vs-unset.
# Everything else in braces (${VAR:?required}, ${VAR}, ${VAR%...}, ${VAR#...}) IS a reference:
# live-found 2026-09-25 — sudoers-rule checks "${RULE:?RULE is required}", which the
# brace-closed regex never matched, so the lint silently passed an undeclared var
# and the plant failed rc=2 in 0s. live-found 2026-09-26 — only `:-` was stripped, so the
# common ${VAR-} optional form (local-user GROUPS_ADD, systemd-service PAYLOAD_*/EXTRA_*,
# apache-site EXTRA_DIRECTIVES) false-flagged a valid deploy.
_BASH_DEFAULT_RE = re.compile(r"\$\{[A-Z_][A-Z0-9_]*:?[-+=][^}]*\}")
_BASH_VAR_RE = re.compile(r"\$(?:\{([A-Z_][A-Z0-9_]*)|([A-Z_][A-Z0-9_]*))")


def build_nakon_bundle(config_path):
    """Build (or reuse) the content-addressed Nakon bundle for this competition."""
    with _BUNDLE_BUILD_LOCK:
        result = subprocess.run(
            [sys.executable, "-m", "nakon", "build",
             "--config", str(Path(config_path).resolve()),
             "--out", "bundles",
             "--json"],
            cwd=str(NAKON_DIR), capture_output=True, text=True, timeout=900,
        )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError(
            "nakon build failed — the vulndb (MySQL + vulndb-ui) has to be reachable from "
            "this machine. Check vendor/nakon/.env."
        )

    info = json.loads(result.stdout.strip().splitlines()[-1])
    state = "cached" if info["cached"] else "fresh"
    print(f"  Nakon bundle {info['bundle_id'][:12]} ({state}, {info['plans']} plan(s), "
          f"{info['machines']} machine(s))")
    bundle_path = NAKON_DIR / info["path"]
    _lint_bundle_vars(bundle_path)
    return bundle_path


def _lint_bundle_vars(bundle_path):
    """Drift guard for the curated REQUIRED_VARS table (M4).

    The catalog DB exposes no required-vars metadata, so the table can silently go
    stale as the catalog grows. The bundle manifest knows exactly which vars each
    step receives and which sha256-addressed script blob it runs — scanning those
    blobs for uppercase `$VAR` references not declared by the step (and not a shell
    builtin) fails the deploy at bundle-build time, before any plant time is spent.
    Remediations: pin the var ({name, vars}), declare it identity-derived in
    REQUIRED_VARS, or (shell builtins only) extend the lint whitelist."""

    def _undeclared(text, provided):
        # Single-quoted spans never expand ('$TTL 604800' is a DNS zone directive, not
        # a var); ${VAR:-default} self-defaults are fine undeclared. The single-quote
        # strip must NOT cross newlines — an apostrophe in a comment (# set the user's
        # wallpaper) otherwise swallows real code below it, including the very VAR=
        # assignment that would exempt a var (live-found 2026-09-26: theme-wallpaper's
        # DEST="$DEST_DIR/..." was eaten, false-flagging DEST).
        # Full-line shell comments never execute — a line whose first non-blank char is
        # `#` is unambiguously a comment (unlike ${VAR#x} or $#, which aren't line-leading).
        # Drop them so prose mentioning a var (theme-motd's `printf '%s\n' "$VAR"` docstring)
        # isn't read as a reference (live-found 2026-09-26).
        text = re.sub(r"(?m)^[ \t]*#.*$", "", text)
        text = re.sub(r"'[^'\n]*'", "''", text)
        # A `[ -z "$VAR" ]` / `[ -n "$VAR" ]` guard (bare or braced, ${VAR-} included) means
        # the script defaults or makes the var optional itself. Scan for it BEFORE stripping
        # self-defaults — otherwise the ${VAR-} inside the guard is removed first and the
        # guard's var is lost (live-found 2026-09-26: GROUPS_ADD/PAYLOAD_PATH/EXTRA_*).
        guarded = {m.group(1) for m in re.finditer(
            r"\[\s+-[zn]\s+\"\$\{?([A-Za-z_][A-Za-z0-9_]*)", text)}
        text = _BASH_DEFAULT_RE.sub("", text)
        # Variables the script assigns itself (IFACE=$(ip route ...)) are internal;
        # leading-underscore names are bash specials or PHP globals ($_GET) in heredocs.
        assigned = {m.group(1) for m in re.finditer(
            r"(?:^|\n)\s*(?:export\s+|readonly\s+|local\s+|declare\s+-?\w*\s+)?"
            r"([A-Za-z_][A-Za-z0-9_]*)=", text)}
        refs = set()
        for m in _BASH_VAR_RE.finditer(text):
            name = m.group(1) or m.group(2)
            if name.startswith("_"):
                continue
            if name in provided or name in _SHELL_VAR_WHITELIST or name in assigned or name in guarded:
                continue
            refs.add(name)
        return refs

    try:
        manifest = json.loads((bundle_path / "manifest.json").read_text())
    except (OSError, ValueError):
        return  # no manifest to lint against (caller fails later on the real run)
    violations = []
    for plan_id, plan in (manifest.get("plans") or {}).items():
        if plan.get("platform") != "linux":
            continue  # windows payloads use different var syntax ($env:, PS casing)
        for step in plan.get("steps", []):
            blob = bundle_path / "blobs" / (step.get("script_sha256") or "")
            try:
                text = blob.read_text(errors="replace")
            except OSError:
                continue
            refs = _undeclared(text, set((step.get("vars") or {}).keys()))
            if refs:
                violations.append((step.get("name") or plan_id[:12], sorted(refs)))
    if violations:
        lines = "\n".join(f"    {name}: {', '.join(vars_)}" for name, vars_ in violations)
        raise SystemExit(
            "  ERROR: catalog payload(s) reference variable(s) their step does not "
            "declare — they would fail mid-plant:\n" + lines +
            "\n  Pin the var(s) in box_services.json/box_vulns.json as "
            '{"name": ..., "vars": {...}}, declare identity-derived ones in '
            "constants.REQUIRED_VARS, or extend the lint whitelist if they are shell "
            "builtins. See docs/known-issues.md 'randomize-to-pin' entry.")
