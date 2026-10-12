"""TeardownPlan: ordered steps as data, validated at construction.

The invariant that used to live in comments — evidence collection happens
before anything destructive — is a construction rule here: a TERMINAL_TEARDOWN
plan with a destructive step before an EVIDENCE step cannot be BUILT, so no
mode added later can reintroduce the ordering races of 2026-10-09.

Two kinds:
  TERMINAL_TEARDOWN — the end-of-range teardown (destroy-competition.py). The
      evidence-ordering rule applies.
  RECLAIM — phase 1 destroying the PRIOR run's leftovers before a fresh deploy.
      Evidence steps are exempt by design: the prior run's evidence was that
      run's teardown's job; reclaim only reclaims."""

EVIDENCE = "evidence"
TEARDOWN = "teardown"

TERMINAL_TEARDOWN = "terminal_teardown"
RECLAIM = "reclaim"


class PlanError(ValueError):
    pass


class TeardownPlan:
    def __init__(self, kind, gates, steps):
        self.kind = kind
        self.gates = list(gates)          # callables(ctx) -> None, raise Refusal
        self.steps = list(steps)
        self._validate()

    def _validate(self):
        if self.kind == RECLAIM:
            return
        evidence_idx = [i for i, s in enumerate(self.steps) if s.kind == EVIDENCE]
        if not evidence_idx:
            raise PlanError(
                f"a {TERMINAL_TEARDOWN} plan needs at least one EVIDENCE step "
                f"(evidence before destruction is a construction rule, not a habit)")
        last_evidence = max(evidence_idx)
        for i, s in enumerate(self.steps):
            if s.kind == TEARDOWN and s.destructive and i < last_evidence:
                raise PlanError(
                    f"plan order violates evidence-before-destruction: '{s.name}' "
                    f"(destructive) at position {i} precedes an EVIDENCE step at "
                    f"position {last_evidence}")
