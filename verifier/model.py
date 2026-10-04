"""Gate result types: the tri-state Status, GateResult, and the constructors every gate returns."""

import dataclasses
import enum


class CheckError(Exception):
    """A check could not run (missing data / unreachable infra) — clean message, no trace."""


class Status(enum.Enum):
    """Tri-state gate verdict.

    SKIP_UNAVAILABLE means the gate could not be evaluated (dead SSH, missing
    state, no vantage point) and is deliberately NOT a pass: a check that never
    ran exiting 0 is how a dead box reads as a healthy range (live-found
    2026-10-02: isolation's cross-team probe "PASSed" on stopped VMs). The
    operator can waive a specific gate with --allow-unverified."""
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP_UNAVAILABLE = "SKIP"


@dataclasses.dataclass
class GateResult:
    """One gate's verdict — the SUMMARY and the exit code both read THIS object,
    so the printed word and the gate dict can never drift apart (the old
    hand-printed SUMMARY said "SKIP — unverified" for isolation while the dict
    recorded a FAIL)."""
    name: str
    status: Status
    detail: str = ""
    gating: bool = True      # False = reported in SUMMARY, excluded from the verdict
    label: str = ""          # SUMMARY label; defaults to name

    def __post_init__(self):
        if not self.label:
            self.label = self.name

    @property
    def passed(self):
        return self.status is Status.PASS


def gate_pass(name, detail="", gating=True, label=""):
    return GateResult(name, Status.PASS, detail, gating, label)


def gate_fail(name, detail="", gating=True, label=""):
    return GateResult(name, Status.FAIL, detail, gating, label)


def gate_skip(name, detail="", gating=True, label=""):
    return GateResult(name, Status.SKIP_UNAVAILABLE, detail, gating, label)


def bool_gate(name, ok, detail="", gating=True, label=""):
    """Wrap a plain bool check (no SKIP outcome) as a GateResult."""
    return GateResult(name, Status.PASS if ok else Status.FAIL, detail, gating, label)
