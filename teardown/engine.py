"""TeardownEngine: gates → journal → steps → verify_clean().

The engine is the one place that applies step policies (REFUSE vs
WARN_AND_PROCEED), the one place that journals completion
(competitions/<id>/.teardown-journal.json — resume skips journalled, resumable
steps, which is what makes re-running until clean both safe and cheap), and
the only source of a clean verdict. Exit 0 means verify_clean said clean;
there is no other exit-0 path."""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

TEARDOWN_JOURNAL = ".teardown-journal.json"


@dataclass
class CleanReport:
    clean: bool
    problems: list = field(default_factory=list)   # must-fix (exit nonzero)
    notes: list = field(default_factory=list)      # kept-by-design / foreign survivors

    def render(self):
        lines = []
        for p in self.problems:
            lines.append(f"  NOT CLEAN: {p}")
        for n in self.notes:
            lines.append(f"  note: {n}")
        if self.clean:
            lines.append("  estate clean for this ownership set.")
        return "\n".join(lines)


class TeardownEngine:
    def __init__(self, ctx, journal_path=None, log=print, journal=True):
        self.ctx = ctx
        self.journal_enabled = journal
        self.journal_path = Path(journal_path) if journal_path \
            else Path(ctx.comp_dir) / TEARDOWN_JOURNAL
        self.log = log

    def _load_journal(self):
        if not self.journal_enabled:
            return {"run_id": self.ctx.ownership.run_id, "done": {}}
        try:
            data = json.loads(self.journal_path.read_text())
            if isinstance(data, dict) and data.get("done"):
                return data
        except (OSError, ValueError):
            pass
        return {"run_id": self.ctx.ownership.run_id, "done": {}}

    def execute(self, plan):
        journal = self._load_journal()
        if journal.get("run_id") not in (None, self.ctx.ownership.run_id):
            self.log(f"  journal belongs to run {journal.get('run_id')} — starting fresh")
            journal = {"run_id": self.ctx.ownership.run_id, "done": {}}
        results = []
        for gate in plan.gates:
            gate(self.ctx)                      # raises Refusal — before ANY mutation
        for step in plan.steps:
            done = (journal["done"] or {}).get(step.name)
            if step.resumable and done:
                results.append(self._result(step, "skipped",
                                            f"already done this run ({done.get('detail', '')})"))
                self.log(f"  [{step.name}] skipped — journalled done")
                continue
            self.ctx.inventory.invalidate()     # every step sees fresh state
            try:
                result = step.run(self.ctx)
            except Exception as e:              # noqa: BLE001 - policy applies here, once
                from infra.ownership import Refusal
                kind = "refused" if isinstance(e, Refusal) else type(e).__name__
                if step.failure == "refuse":
                    result = self._result(step, "failed", f"{kind}: {e}")
                else:
                    result = self._result(step, "warned", f"{kind}: {e} — continuing")
            results.append(result)
            self.log(f"  [{step.name}] {result.status}"
                     + (f" — {result.detail}" if result.detail else ""))
            if result.status in ("ok", "skipped"):   # warned/failed: a re-run retries it
                journal["done"][step.name] = step.journal_detail(result)
                if self.journal_enabled:
                    journal["run_id"] = self.ctx.ownership.run_id
                    journal["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    from config.config_ops import write_text_atomic
                    write_text_atomic(self.journal_path, json.dumps(journal, indent=2),
                                      mode=0o600)
            if result.status == "failed" and step.failure == "refuse":
                break
        return results

    @staticmethod
    def _result(step, status, detail=""):
        from teardown.steps import StepResult
        return StepResult(step.name, status, detail)

    # the exit contract

    # unverifiable is NOT clean — fail closed on listing errors
    def verify_clean(self, plan, results=None):
        from infra.ownership import parse_vm_tags

        inv = self.ctx.inventory
        inv.invalidate()
        problems, notes = [], []

        # 0. the plan must have COMPLETED — a step that refused means the estate
        # was not fully processed, whatever the current listing says
        if results is not None:
            failed = [r for r in results if r.status == "failed"]
            if failed:
                problems.append(f"teardown step '{failed[0].name}' failed — the plan did "
                                f"not complete; re-run the destroy (it is idempotent)")
            elif len(results) < len(plan.steps):
                problems.append("teardown stopped early — not every step ran; "
                                "re-run the destroy (it is idempotent)")

        # 1. fail closed on nodes we could not see
        for node, err in inv.listing_failed():
            problems.append(f"could not list VMs on {node} ({err}) — cleanliness "
                            f"unverifiable, which is not clean")

        # 2. nothing of ours remains (mode-dependent)
        owned = inv.owned(self.ctx.ownership)
        role_tags = {"tezcatlipoca-golden", "engine-template"}
        for node, v in owned:
            tags = parse_vm_tags(v.get("tags"))
            kept_by_design = not self.ctx.full and bool(tags & role_tags)
            if self.ctx.full or not kept_by_design or v.get("status") == "running":
                problems.append(f"our VM still standing on {node}: {v.get('name')} "
                                f"(vmid {v['vmid']}, status {v.get('status')})")
            else:
                notes.append(f"templates kept for THIS competition's next run: "
                             f"{v.get('name')} (vmid {v['vmid']}) on {node}")

        # 3. foreign survivors are named, never touched
        for node, v in inv.comp_tagged(self.ctx.ownership.comp):
            tags = parse_vm_tags(v.get("tags"))
            if set(self.ctx.ownership.tags) <= tags:
                continue                        # already reported as ours above
            if any(t.startswith("run-") for t in tags):
                notes.append(f"same competition, DIFFERENT run — not touched by this "
                             f"teardown: {v.get('name')} (vmid {v['vmid']}) on {node}")
            else:
                notes.append(f"competition-tagged, carries no run tag: "
                             f"{v.get('name')} (vmid {v['vmid']}) on {node}")

        # 4. red01 gone (when it existed and we were told to destroy it)
        if not self.ctx.keep_red:
            # the vmid: what RedStep actually destroyed (journal), then bad-auto's config, then the deploy default; None when unverifiable
            done_vmid = (self._journal_done(plan).get("red01") or {}).get("vmid")
            from red.red_plant_ops import red_vmid_candidates
            vmid = None
            for candidate in [done_vmid] + list(
                    red_vmid_candidates(self.ctx.comp_dir, self.ctx.deployed_state)):
                if candidate:
                    vmid = candidate
                    break
            still = None
            red01_done = self._journal_done(plan).get("red01")
            if vmid:
                try:
                    from infra.client import live_vmids
                    still = int(vmid) in live_vmids()
                except Exception:            # noqa: BLE001 - unverifiable is not clean
                    still = None
            if still is True:
                problems.append("red01 is STILL PRESENT — it holds the LLM key and "
                                "beacon tasking")
            elif still is None and (self.ctx.deployed_state.get("assume_breach")
                                    or (red01_done and red01_done.get("status") != "skipped")):
                problems.append("red01's vmid could not be verified gone — check by hand")

        # 5. no terraform state lock left behind
        lock = Path(self.ctx.comp_dir) / "terraform" / ".terraform.tfstate.lock.info"
        if lock.exists():
            problems.append(f"{lock} still present — a terraform process may be alive; "
                            f"settle it and re-run")

        # 6. evidence archived — demanded only when a teardown actually ran
        # (a fresh journal means this plan never got past its gates)
        done = self._journal_done(plan)
        if done and any(s.kind == "evidence" for s in plan.steps):
            record = done.get("artifacts") or {}
            test_dir = record.get("test_dir")
            if not test_dir or not Path(test_dir).exists():
                problems.append("no artifact folder for this run — evidence was not "
                                "archived before destruction")

        return CleanReport(not problems, problems, notes)

    def _journal_done(self, plan):
        return self._load_journal().get("done") or {}
