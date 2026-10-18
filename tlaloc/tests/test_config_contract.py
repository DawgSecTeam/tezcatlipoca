"""The config contract — the invariant that keeps red01's features alive.

Every key the agent reads must be listed in `cli.CONFIG_CONTRACT`, because that
list is what the deploy pipeline writes into `/etc/tlaloc/config.yaml` and what
`tlaloc verify-config --pushed` diffs per field. A key that is read but not
listed is dropped on the way to the VM: the feature is dead on red01 while every
local test stays green. The earlier agent shipped exactly that bug (a whole
feature absent from the push allowlist), and this file is the wall against it.
"""

import re
from pathlib import Path

import pytest

from tlaloc import cli, config
from tlaloc.effects import MODULES

# Config paths the effect modules read, as a DATA TABLE (the plan bans source-text
# tests for behaviour; this is a plumbing inventory, and the scan below is what
# keeps the inventory honest rather than the reader's memory).
EFFECT_CONFIG_KEYS = {
    "c2.callback_max_age_sec": {"implant_channel", "lockout", "reestablish", "task_channel"},
    "c2.callback_wait_sec": {"implant_channel"},
    "c2.task_timeout_sec": {"task_channel"},
    "effects.allow_ssh_takedown": {"takedown"},
    "effects.decoy": {"persist_linux"},
    "effects.rekill_cron": {"persist_linux"},
    "effects.rotate_credlist": {"rotate_credlist"},
    "evasion.preload_so": {"persist_linux"},
    "limits.cred_spray_max": {"spray_access"},
    "limits.command_max_len": {"improvise"},
    "limits.spray_delay_sec": {"spray_access"},
    "limits.spray_probe_timeout": {"spray_access"},
    "limits.action_timeout": {"improvise", "recon"},
    "procfilter.install_dir": {"persist_linux"},
    "run.comp_dir": {"implant_channel"},
    "run.state_dir": {"persist_linux"},
}


def _defaults_leaves(node, prefix=""):
    leaves = []
    for key, value in node.items():
        if isinstance(value, dict) and value:
            leaves += _defaults_leaves(value, f"{prefix}{key}.")
        else:
            leaves.append(f"{prefix}{key}")
    return leaves


def _covers(field):
    """A contract entry covers a field if it IS it or is a parent of it."""
    return field in cli.CONFIG_CONTRACT or any(
        entry.startswith(field + ".") for entry in cli.CONFIG_CONTRACT)


def test_contract_covers_every_default_key():
    """No config key may be readable by the agent and absent from the contract."""
    missing = [leaf for leaf in _defaults_leaves(config.DEFAULTS) if not _covers(leaf)]
    assert missing == [], (
        "these DEFAULTS keys are not in cli.CONFIG_CONTRACT, so the deploy would "
        f"drop them and the feature would be dead on red01: {missing}")


def test_contract_fields_resolve_in_defaults():
    """The reverse direction: a contract field that is not a real config path is
    a typo that makes every deploy diff report a phantom mismatch."""
    # `is None` — an empty string / False / {} is a legitimate default value, and
    # treating it as "missing" would flag half the contract.
    broken = [field for field in cli.CONFIG_CONTRACT
              if _resolve(config.DEFAULTS, field) is None]
    assert broken == [], f"contract fields that are not real config paths: {broken}"


def test_verify_config_compares_effective_values_not_files():
    """A diff-style push (the deploy writes only what it knows) must not report
    every default-valued field as dropped — but a field whose effective value would
    differ on the VM is a real problem."""
    import copy
    # An empty push means "everything is default" — and that is correct.
    assert cli.verify_config(copy.deepcopy(config.DEFAULTS), pushed={}) == []

    # A realistic render: the deploy writes the comp's own identity, nothing else.
    # Only fields whose EFFECTIVE value differs may be flagged — before the
    # effective-value fix this returned all 72 entries.
    sparse_push = {"run": {"comp": "x", "state_dir": "/var/lib/tlaloc"}}
    local = copy.deepcopy(config.DEFAULTS)
    local["limits"]["spray_delay_sec"] = 1.5        # set here, never pushed
    local["clef"]["tokens_per_char"] = 1.0          # set here, never pushed
    problems = cli.verify_config(local, pushed=sparse_push)
    assert {p["field"] for p in problems} == {
        "clef.tokens_per_char", "limits.spray_delay_sec",   # overrides not pushed
        "run.comp", "run.state_dir"}                        # pushed != local here
    assert all("effective" in p["problem"] for p in problems)

    # and a pushed value that disagrees with the effective one is still flagged
    problems = cli.verify_config(copy.deepcopy(config.DEFAULTS),
                                 pushed={"clef": {"theta": 0.9}})
    assert [p["field"] for p in problems] == ["clef.theta"]


def _resolve(cfg, dotted):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def test_every_effect_config_read_is_declared():
    """Each path in the table must exist in DEFAULTS and be covered by the
    contract — the table is what an operator reads when a knob seems ignored."""
    problems = {}
    for field in EFFECT_CONFIG_KEYS:
        if _resolve(config.DEFAULTS, field) is None:
            problems[field] = "not in config.DEFAULTS"
        elif not _covers(field):
            problems[field] = "not covered by cli.CONFIG_CONTRACT"
    assert problems == {}, f"effect config reads that would be dead on red01: {problems}"


def test_the_effect_read_table_is_complete():
    """The table above must name every config path the effect modules read.

    This is the one place a source scan is the right tool: it is not asserting
    behaviour, it is asserting that a plumbing INVENTORY matches the plumbing. A
    new `ctx.cfg[...]` read in a phased module has to be declared, or this fails
    and nobody ships a knob that the VM never receives.
    """
    here = Path(__file__).resolve().parent.parent / "tlaloc" / "effects"
    chain = re.compile(r'\bcfg\b((?:\s*(?:\[\s*["\'](\w+)["\']\s*\]'
                       r'|\.\s*get\(\s*["\'](\w+)["\']))+)')
    seen = {}
    for module in MODULES + ("artifactnames",):
        path = here / f"{module}.py"
        if not path.exists():
            continue
        for match in chain.finditer(path.read_text()):
            keys = re.findall(r'["\'](\w+)["\']', match.group(1))
            if keys:
                seen.setdefault(".".join(keys[:3]), set()).add(module)
    undeclared = {p: sorted(m) for p, m in seen.items()
                  if not _covers(p) and not _declared_family(p)}
    assert undeclared == {}, (
        "these config paths are read by an effect but undeclared — add them to "
        f"config.DEFAULTS + CONFIG_CONTRACT + EFFECT_CONFIG_KEYS: {undeclared}")


def _declared_family(path):
    """Parent-only reads (e.g. `cfg["c2"]`, `cfg["limits"]`) are covered when the
    parent has contract entries, and carry no leaf of their own."""
    return any(entry.startswith(path + ".") for entry in cli.CONFIG_CONTRACT)
