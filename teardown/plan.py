"""TeardownPlan: ordered steps as data, validated at construction (docs/internals.md#teardown)."""

EVIDENCE = "evidence"
TEARDOWN = "teardown"


class PlanError(ValueError):
    pass


class TeardownPlan:
    def __init__(self, gates, steps):
        self.gates = list(gates)
        self.steps = list(steps)
        evidence_idx = [i for i, s in enumerate(self.steps) if s.kind == EVIDENCE]
        if not evidence_idx:
            raise PlanError(
                "a teardown plan needs at least one EVIDENCE step "
                "(evidence before destruction is a construction rule, not a habit)")
        last_evidence = max(evidence_idx)
        for i, s in enumerate(self.steps):
            if s.kind == TEARDOWN and s.destructive and i < last_evidence:
                raise PlanError(
                    f"plan order violates evidence-before-destruction: '{s.name}' "
                    f"(destructive) at position {i} precedes an EVIDENCE step at "
                    f"position {last_evidence}")
