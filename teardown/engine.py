"""TeardownEngine: gates → journal → steps → verify_clean()."""

import time
from dataclasses import dataclass, field
from pathlib import Path

from common.utils import competition_name

def load_teardown_journal(comp_dir):
    from estate import state
    section = state.EstateState.load(comp_dir).section("teardown")
    return section if section.get("done") else {}


def save_teardown_journal(comp_dir, journal):
    from estate import state
    document = state.EstateState.load(comp_dir)
    document.section("teardown").update(journal)
    document.save()


@dataclass
class CleanReport:
    clean: bool
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)

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
    def __init__(self, ctx, log=print, journal=True):
        self.ctx = ctx
        self.journal_enabled = journal
        self.log = log

    def _load_journal(self):
        if not self.journal_enabled:
            return {"run_id": self.ctx.ownership.run_id, "done": {}}
        data = load_teardown_journal(self.ctx.comp_dir)
        if isinstance(data, dict) and data.get("done"):
            return data
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
            self.ctx.inventory.invalidate()
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
            if result.status in ("ok", "skipped"):
                journal["done"][step.name] = step.journal_detail(result)
                if self.journal_enabled:
                    journal["run_id"] = self.ctx.ownership.run_id
                    journal["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    save_teardown_journal(self.ctx.comp_dir, journal)
            if result.status == "failed" and step.failure == "refuse":
                break
        return results

    @staticmethod
    def _result(step, status, detail=""):
        from teardown.steps import StepResult
        return StepResult(step.name, status, detail)

    # unverifiable is NOT clean — fail closed on listing errors
    def verify_clean(self, plan, results=None):
        from infra.ownership import parse_vm_tags

        inv = self.ctx.inventory
        inv.invalidate()
        problems, notes = [], []

        # a step that refused means the estate was not fully processed, whatever the
        # current listing says
        if results is not None:
            failed = [r for r in results if r.status == "failed"]
            if failed:
                problems.append(f"teardown step '{failed[0].name}' failed — the plan did "
                                f"not complete; re-run the destroy (it is idempotent)")
            elif len(results) < len(plan.steps):
                problems.append("teardown stopped early — not every step ran; "
                                "re-run the destroy (it is idempotent)")

        for node, err in inv.listing_failed():
            problems.append(f"could not list VMs on {node} ({err}) — cleanliness "
                            f"unverifiable, which is not clean")

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
                continue
            if any(t.startswith("run-") for t in tags):
                notes.append(f"same competition, DIFFERENT run — not touched by this "
                             f"teardown: {v.get('name')} (vmid {v['vmid']}) on {node}")
            else:
                notes.append(f"competition-tagged, carries no run tag: "
                             f"{v.get('name')} (vmid {v['vmid']}) on {node}")

        if not self.ctx.keep_red:
            # vmid: what RedStep destroyed (journal), then bad-auto's config, then the deploy default
            done_vmid = (self._journal_done(plan).get("red01") or {}).get("vmid")
            from red.red_plant_ops import red_vmid_candidates
            vmid = None
            for candidate in [done_vmid] + list(
                    red_vmid_candidates(self.ctx.comp_dir, self.ctx.deployed_state)):
                if candidate:
                    vmid = candidate
                    break
            still = None
            verdict = None
            red01_done = self._journal_done(plan).get("red01")
            if vmid:
                try:
                    from infra import red01
                    verdict = red01.occupant_verdict(vmid, competition_name(self.ctx.comp_dir))
                except Exception:            # noqa: BLE001 - unverifiable is not clean
                    verdict = None
                still = verdict in ("ours", "unmarked")
            if verdict == "ours":
                problems.append("red01 is STILL PRESENT — it holds the LLM key and "
                                "beacon tasking")
            elif verdict == "foreign":
                # marker names another competition: our red01 is gone and the vmid was recycled
                notes.append(f"vmid {vmid} now holds another competition's red01 "
                             f"({red01.occupant_label(vmid)}) — this range's red01 is gone")
            elif verdict == "unmarked":
                problems.append(f"a VM occupies red01's vmid {vmid} with no bad-auto marker "
                                f"({red01.occupant_label(vmid)}) — its owner cannot be "
                                f"attributed, so it is not proven gone")
            elif still is None and (self.ctx.deployed_state.get("assume_breach")
                                    or (red01_done and red01_done.get("status") != "skipped")):
                problems.append("red01's vmid could not be verified gone — check by hand")

        done = self._journal_done(plan)
        if done and any(s.kind == "evidence" for s in plan.steps):
            record = done.get("artifacts") or {}
            test_dir = record.get("test_dir")
            if not test_dir or not Path(test_dir).exists():
                problems.append("no artifact folder for this run — evidence was not "
                                "archived before destruction")
            else:
                # a note, never a problem: a wedged box must not hold a destroy hostage
                from artifacts_lib.collect import load_collection
                from artifacts_lib.constants import LOST
                lost = [f"{t.get('name')}/{f.get('want')} ({f.get('status')})"
                        for t in load_collection(test_dir).get("targets", [])
                        for f in t.get("files", []) if f.get("status") in LOST]
                if lost:
                    notes.append(f"evidence incomplete: {len(lost)} item(s) lost "
                                 f"({', '.join(lost[:4])}{', …' if len(lost) > 4 else ''}) — "
                                 f"see {test_dir}/collection.json")

        return CleanReport(not problems, problems, notes)

    def _journal_done(self, plan):
        return self._load_journal().get("done") or {}
