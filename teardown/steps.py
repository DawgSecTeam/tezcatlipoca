"""Steps: the units a teardown plan is made of."""

import re
from dataclasses import dataclass
from pathlib import Path

from engine.template_freeze import forget_template_hashes
from common.utils import competition_name

EVIDENCE = "evidence"
TEARDOWN = "teardown"

REFUSE = "refuse"
WARN_AND_PROCEED = "warn"


# the truthful vmid: config.yaml is a shared checkout whose deploy.red_vmid another session's deploy can rewrite mid-flight; the target line is what the guards verified and what got deleted
def _red_target_vmid(proc):
    m = re.search(r"destroy target: .*\(vmid (\d+)\)", (proc.stdout or "") + "\n"
                  + (proc.stderr or ""))
    return int(m.group(1)) if m else None


@dataclass
class StepResult:
    name: str
    status: str          # ok | warned | failed | skipped
    detail: str = ""


@dataclass
class TeardownContext:
    comp_dir: Path
    env: dict
    teams: dict
    boxes: list
    deployed_state: dict
    ownership: object                  # vm_ownership.Ownership
    placement: dict | None
    nodes: list                        # every hosting node (engine + satellites)
    default_node: str
    inventory: object                  # teardown.inventory.Inventory
    full: bool = True
    keep_red: bool = False
    artifacts_timeout: int = 45


class Step:
    name = "step"
    kind = TEARDOWN
    failure = WARN_AND_PROCEED
    destructive = False
    resumable = False

    def run(self, ctx) -> StepResult:
        raise NotImplementedError

    def journal_detail(self, result) -> dict:
        return {"detail": result.detail}


# save the access log and revoke this run's console user before artifact collection (the log rides it) and while the engine still answers
class PortalStep(Step):
    name = "portal"

    def run(self, ctx):
        from portal.portal_ops import portal_enabled, teardown_portal
        from estate import config as estate_config
        if not (ctx.deployed_state.get("portal_console_tokens")
                or ctx.deployed_state.get("portal_up") or portal_enabled(ctx.comp_dir)):
            return StepResult(self.name, "ok", "portal not deployed")
        try:
            engine_ctx = {
                "scoring_engine_ip": ctx.deployed_state.get("engine_mgmt_ip") or "",
                "ssh_key_path": str(estate_config.ssh_key_path()),
                "vm_username": estate_config.active().engine.username,
            }
            teardown_portal(ctx.comp_dir, ctx.ownership.comp, ctx.ownership.run_id,
                            ctx.deployed_state, ctx.teams, ctx.boxes, ctx.placement,
                            ctx.default_node, tf_ctx=engine_ctx)
            return StepResult(self.name, "ok")
        except Exception as e:           # noqa: BLE001 - warn-and-proceed by policy
            return StepResult(self.name, "warned",
                              f"portal teardown failed ({type(e).__name__}: {e}) — "
                              f"re-run the destroy to retry the console-user removal")


# The engine's final scoreboard lives only in its database, and EstateStep deletes that database:
# capture it here, before anything destructive, for the run whose harness died before its own capture.
class EngineCaptureStep(Step):
    name = "engine_capture"
    kind = EVIDENCE
    resumable = True

    def run(self, ctx):
        from artifacts_lib.lifecycle import test_folder_for_teardown
        from artifacts_lib.manifest import record_paths
        from quotient.event_capture import dump_scoreboard, login_scoring
        ip = ctx.deployed_state.get("engine_mgmt_ip") or ""
        if not ip:
            return StepResult(self.name, "ok", "no engine management IP recorded")
        password = ctx.deployed_state.get("scoring_password")
        if not password:
            return StepResult(self.name, "warned",
                              "no scoring password in the state — the engine's final "
                              "scoreboard was not captured before the destroy")
        base_url = ip if ip.startswith("http") else f"http://{ip}"
        path, manifest = test_folder_for_teardown(
            ctx.comp_dir, run_id=ctx.ownership.run_id, teams=ctx.teams, boxes=ctx.boxes,
            node=ctx.default_node, nodes=ctx.nodes)
        recorded = (manifest.get("paths") or {}).get("engine_evidence")
        if recorded and Path(recorded).is_dir() and any(Path(recorded).iterdir()):
            return StepResult(self.name, "skipped", f"harness capture present at {recorded}")
        # Never the directory the collector swaps into: a local seal unlinks its destination
        # first, so capturing into the collector's own target would delete the file just written.
        ev = ctx.comp_dir / "evidence"
        ev.mkdir(parents=True, exist_ok=True)
        try:
            errors = dump_scoreboard(base_url, login_scoring(base_url, password),
                                     ctx.comp_dir, out_dir=ev)
        except Exception as e:           # noqa: BLE001 - warn-and-proceed by policy
            return StepResult(self.name, "warned",
                              f"engine capture failed ({type(e).__name__}: {e})")
        record_paths(path, engine_evidence=ev)
        return StepResult(self.name, "ok", str(ev)
                          + (f" ({len(errors)} capture error(s))" if errors else ""))


class ArtifactStep(Step):
    name = "artifacts"
    kind = EVIDENCE
    resumable = True

    def run(self, ctx):
        from artifacts_lib import artifacts_ops
        from artifacts_lib.lifecycle import latest_run_key
        from artifacts_lib.paths import test_dir
        artifacts_ops.collect_for_teardown(
            ctx.comp_dir, run_id=ctx.ownership.run_id, teams=ctx.teams, boxes=ctx.boxes,
            node=ctx.default_node, nodes=ctx.nodes,
            script="destroy-competition.py", timeout=ctx.artifacts_timeout)
        key, _ = latest_run_key(ctx.comp_dir, ctx.ownership.run_id)
        return StepResult(self.name, "ok", str(test_dir(ctx.comp_dir, key)))

    def journal_detail(self, result):
        return {"detail": result.detail, "test_dir": result.detail}


def _log(msg):
    print(msg)


class RedStep(Step):
    """red01 is bad-auto's VM; reaching the standalone teardown with it alive means the harness died."""
    name = "red01"
    destructive = True
    resumable = True

    def run(self, ctx):
        from red import red_plant_ops, tlaloc_ops
        if ctx.keep_red:
            return StepResult(self.name, "skipped", "--keep-red")
        candidates = red_plant_ops.red_vmid_candidates(ctx.comp_dir, ctx.deployed_state)
        if not candidates:
            # No per-comp bad-auto config and no recorded vmid: `badauto destroy` would aim at the
            # HOST's default config (red_vmid 999 — caught live: it destroyed another session's red01).
            return StepResult(self.name, "skipped",
                              "no per-comp bad-auto config and no recorded red vmid — "
                              "this run staged no red agent")
        print("  red01      : badauto destroy (not estate-managed — this script cannot "
              "remove it any other way)...")
        if tlaloc_ops.red_engine(ctx.comp_dir) == "tlaloc":
            r = tlaloc_ops.destroy_tlaloc_red(ctx.comp_dir, env=ctx.env)
        else:
            r = red_plant_ops.destroy_red(ctx.comp_dir, env=ctx.env)
        tail = " ".join(((r.stderr or "") + (r.stdout or "")).split())[-200:]
        self.last_vmid = _red_target_vmid(r) or (
            red_plant_ops.red_vmid_candidates(ctx.comp_dir, ctx.deployed_state) or [None])[0]
        if r.returncode == 0:
            return StepResult(self.name, "ok", tail or "no output")
        refusal = red_plant_ops.parse_teardown_refusal(r)
        if refusal:
            # bad-auto refuses without ITS marker, and a deploy that died before stamping leaves
            # exactly that — a red01 no destroy will claim. Reclaim narrowly, then FAIL if still there.
            from infra import red01
            vmid = self.last_vmid
            # comp_name arms reclaim's another-competition guard: a VM whose marker names a
            # different competition is a recycled vmid, never ours.
            if vmid and red01.reclaim_unstamped_red(vmid, comp_name=competition_name(ctx.comp_dir),
                                                    log=_log):
                return StepResult(self.name, "ok",
                                  f"reclaimed unstamped red01 vmid {vmid} (badauto refused: "
                                  f"{refusal.get('saw')})")
            return StepResult(self.name, "failed",
                              f"badauto destroy REFUSED (rc {r.returncode}) — saw: "
                              f"{refusal.get('saw')}; expected: {refusal.get('expected')}; "
                              f"remedy: {refusal.get('remedy')} — a red box with the LLM key "
                              f"is still up")
        return StepResult(self.name, "warned",
                          f"red01 destroy exited {r.returncode} ({tail}) — check for a "
                          f"surviving red VM by hand (it holds the LLM key)")

    def journal_detail(self, result):
        # status lets verify_clean tell "red01 destroyed" from "skipped — no red staged":
        # only the former demands an absence proof.
        return {"detail": result.detail, "vmid": getattr(self, "last_vmid", None),
                "status": result.status}


class PreStopStep(Step):
    """Hard-stop every team clone: the bpg graceful path hangs on an agent-down DC."""
    name = "pre_stop"
    destructive = True

    def run(self, ctx):
        from teardown.machinery import pre_stop_windows_boxes
        from multinode.placement_record import record_of
        # team_nodes maps teams to RECORD names; the API path needs the PVE node name.
        team_nodes = {k: record_of(ctx.placement, v).node
                      for k, v in (ctx.placement or {}).get("team_nodes", {}).items()} or None
        pre_stop_windows_boxes(ctx.teams, ctx.boxes, ctx.default_node,
                               team_nodes=team_nodes, ownership=ctx.ownership)
        return StepResult(self.name, "ok")


class EstateStep(Step):
    """The recorded estate, destroyed in the one order linked clones survive; REFUSES when an owned resource cannot be destroyed."""
    name = "estate"
    failure = REFUSE
    destructive = True

    def run(self, ctx):
        from estate import destroy as estate_destroy, state as estate_state
        from teardown.machinery import sweep_tagged_leftovers, teardown_templates
        print("\nDestroying this deploy's recorded estate...")
        document = estate_state.EstateState.load(ctx.comp_dir)
        summary = estate_destroy.destroy_estate(document, full=ctx.full, nodes=ctx.nodes)
        if summary["refused"]:
            return StepResult(self.name, "failed", "; ".join(
                f"{r['entry']['kind']} {r['entry']['name']}: {r['reason']}"
                for r in summary["refused"]))
        # A VM created but never recorded (a crash between clone and record) is invisible to the
        # inventory; the run-id-scoped sweep is the net for it.
        sweep_tagged_leftovers(ctx.nodes, ctx.ownership, keep_templates=not ctx.full)
        # A range deployed before the inventory existed has no template records, so its
        # goldens and engine template die by vmid math instead.
        if ctx.full and not (document.resources("golden")
                             or document.resources("engine_template")):
            skipped = teardown_templates(ctx.comp_dir, ctx.deployed_state, ctx.ownership,
                                         ctx.boxes, ctx.placement, full=True)
            if skipped:
                return StepResult(self.name, "warned", "; ".join(skipped))
        if ctx.full:
            forget_template_hashes(ctx.comp_dir)
        return StepResult(self.name, "ok", f"destroyed {len(summary['destroyed'])}")


class RemoteAccessStep(Step):
    """Headscale: revoke the engine's tailnet node, delete this comp's participant users, re-apply the policy; a dead headscale host never blocks destroying a range."""
    name = "remote_access"

    def run(self, ctx):
        from net.remote_access_ops import teardown_remote_access
        teardown_remote_access(ctx.comp_dir, state=ctx.deployed_state)
        return StepResult(self.name, "ok")
