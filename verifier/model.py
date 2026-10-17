"""Gate result types: the tri-state Status, GateResult, and the constructors every gate returns."""

import dataclasses
import enum


class CheckError(Exception):
# A check could not run (missing data / unreachable infra) — clean message, no trace.
    pass


class Status(enum.Enum):
# Tri-state gate verdict: SKIP_UNAVAILABLE is deliberately NOT a pass (a check that never ran
#     exiting 0 is how a dead box reads as a healthy range); --allow-unverified waives one gate.
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP_UNAVAILABLE = "SKIP"


@dataclasses.dataclass
class GateResult:
# The SUMMARY and the exit code both read THIS object, so the printed word and the gate dict
#     cannot drift apart.
    name: str
    status: Status
    detail: str = ""
    gating: bool = True      # False = reported in SUMMARY, excluded from the verdict
    label: str = ""

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
    return GateResult(name, Status.PASS if ok else Status.FAIL, detail, gating, label)
