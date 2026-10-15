"""Steps: the units a teardown plan is made of.

Each step declares — as data, not convention — what it is (EVIDENCE or
TEARDOWN), what may happen when it fails (REFUSE the teardown, or WARN_AND_
PROCEED), whether it destroys range resources (destructive), and whether it is
resumable (skipped when the journal records it done). The EXECUTOR applies the
policies in one place; the steps themselves only do their one job and return a
StepResult. That is the replacement for the copy-pasted try/except forest of
the old sequencings."""

import re
from dataclasses import dataclass
from pathlib import Path

EVIDENCE = "evidence"
TEARDOWN = "teardown"

REFUSE = "refuse"                # a failure aborts the plan (re-enter by re-running)
WARN_AND_PROCEED = "warn"        # a failure is recorded; the teardown continues


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


# built once by the CLI shim / caller; steps must not reach into os.environ or argv themselves
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


# the composition seam for callers whose mechanics live module-side; the policy is declared here, at composition
class CallStep(Step):
    def __init__(self, name, fn, failure=REFUSE, destructive=True):
        self.name = name
        self._fn = fn
        self.failure = failure
        self.destructive = destructive

    def run(self, ctx) -> StepResult:
        detail = self._fn()
        return StepResult(self.name, "ok", str(detail) if detail else "")


# bookkeeping that never outranks destroying the range: save the access log and revoke this run's console user before artifact collection (the log rides it) and while the engine still answers
class PortalStep(Step):
    name = "portal"

    def run(self, ctx):
        from portal.portal_ops import portal_enabled, teardown_portal
        from common.ssh_ops import read_terraform_ctx
        if not (ctx.deployed_state.get("portal_console_tokens")
                or ctx.deployed_state.get("portal_up") or portal_enabled(ctx.comp_dir)):
            return StepResult(self.name, "ok", "portal not deployed")
        try:
            try:
                tf_ctx = read_terraform_ctx(ctx.comp_dir)
            except Exception:            # noqa: BLE001 - no outputs: skip log, still revoke
                tf_ctx = None
            teardown_portal(ctx.comp_dir, ctx.ownership.comp, ctx.ownership.run_id,
                            ctx.deployed_state, ctx.teams, ctx.boxes, ctx.placement,
                            ctx.default_node, tf_ctx=tf_ctx)
            return StepResult(self.name, "ok")
        except Exception as e:           # noqa: BLE001 - warn-and-proceed by policy
            return StepResult(self.name, "warned",
                              f"portal teardown failed ({type(e).__name__}: {e}) — "
                              f"re-run the destroy to retry the console-user removal")


class ArtifactStep(Step):
    """Collect this run's test artifacts BEFORE anything is stopped or deleted.
    The red report and the blue logs only exist while the boxes do; this is also
    the safety net for a run whose harness died. Never blocks: a dead box is
    recorded, not raised (EVIDENCE, warn-and-proceed)."""
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
    """red01 is bad-auto's VM: nothing else in this plan touches it. A normal
    harness run destroys red01 in its own teardown, so reaching the standalone
    teardown with red01 alive means the harness died; leaving it up leaves the
    LLM key and beacon tasking up (scale8 soak). Resumable: a second destroy
    finds nothing, and a journalled first pass proves which vmid died."""
    name = "red01"
    destructive = True
    resumable = True

    def run(self, ctx):
        from red import red_plant_ops
        if ctx.keep_red:
            return StepResult(self.name, "skipped", "--keep-red")
        print("  red01      : badauto destroy (not terraform-managed — this script cannot "
              "remove it any other way)...")
        r = red_plant_ops.destroy_red(ctx.comp_dir, env=ctx.env)
        tail = " ".join(((r.stderr or "") + (r.stdout or "")).split())[-200:]
        self.last_vmid = _red_target_vmid(r) or (
            red_plant_ops.red_vmid_candidates(ctx.comp_dir, ctx.deployed_state) or [None])[0]
        if r.returncode == 0:
            return StepResult(self.name, "ok", tail or "no output")
        refusal = red_plant_ops.parse_teardown_refusal(r)
        if refusal:
            # bad-auto refuses without ITS marker, and a deploy that died before stamping leaves
            # exactly that — a red01 no destroy will claim. Reclaim it narrowly, then FAIL if it
            # is still there: reporting "warned" left the LLM key and beacon tasking up and only
            # surfaced at the verification (2026-10-14, run-df29374d).
            from infra import red01
            vmid = self.last_vmid
            if vmid and red01.reclaim_unstamped_red(vmid, log=_log):
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
        return {"detail": result.detail, "vmid": getattr(self, "last_vmid", None)}


class PreStopStep(Step):
    """Hard-stop every team clone (force by default, owned only): the bpg
    graceful path hangs on an agent-down DC and burns minutes the disk about
    to be destroyed does not need (owner directive 2026-09-30)."""
    name = "pre_stop"
    destructive = True

    def run(self, ctx):
        from teardown.machinery import pre_stop_windows_boxes
        from multinode.placement_record import record_of
        # A placement's team_nodes maps teams to RECORD names; the API path needs the PVE node
        # name (the route table is keyed by it). Unmapped, the pre-stop listed
        # /nodes/<record>/qemu and silently skipped every hard-stop (2026-10-14).
        team_nodes = {k: record_of(ctx.placement, v).node
                      for k, v in (ctx.placement or {}).get("team_nodes", {}).items()} or None
        pre_stop_windows_boxes(ctx.teams, ctx.boxes, ctx.default_node,
                               team_nodes=team_nodes, ownership=ctx.ownership)
        return StepResult(self.name, "ok")


class TerraformStep(Step):
    """`terraform destroy` from the per-comp state dir — bounded attempts with
    stale-lock recovery and the run-id-scoped sweep between them. REFUSES on
    exhaustion: a range still standing is never reported as clean."""
    name = "terraform"
    failure = REFUSE
    destructive = True

    def run(self, ctx):
        from teardown.machinery import destroy_with_recovery, repair_ssh_key_path
        from common.terraform_workdir import terraform_plugin_cache_dir
        tf_cwd = str(ctx.comp_dir / "terraform")
        print(f"\nRunning terraform destroy (state: {tf_cwd})...")
        # Same provider cache the deploy inits with: without it, destroy cannot resolve the
        # providers the per-comp .terraform links point at (2026-10-14).
        env = {**ctx.env, "TF_PLUGIN_CACHE_DIR": str(terraform_plugin_cache_dir())}
        repair_ssh_key_path(Path(tf_cwd) / "terraform.tfvars.json", env)
        if destroy_with_recovery(env, tf_cwd, ctx.nodes, ctx.ownership):
            return StepResult(self.name, "ok")
        return StepResult(self.name, "failed",
                          "terraform destroy did not complete after recovery attempts — "
                          "resolve what verify_clean lists, then re-run (it is safe to "
                          "re-run: teardown is idempotent)")


class TemplatesStep(Step):
    """Golden + jump + engine templates: API-created, not in terraform state,
    the linked clones' base disks — they die only after every clone is gone,
    and only on a full teardown (the default)."""
    name = "templates"
    destructive = True

    def run(self, ctx):
        from teardown.machinery import teardown_templates
        skipped = teardown_templates(ctx.comp_dir, ctx.deployed_state, ctx.ownership,
                                     ctx.boxes, ctx.placement, full=ctx.full)
        if skipped:
            return StepResult(self.name, "warned",
                              f"{len(skipped)} template slot(s) skipped as foreign: "
                              + "; ".join(skipped))
        return StepResult(self.name, "ok")


class RemoteAccessStep(Step):
    """Headscale: revoke the engine's tailnet node, delete this comp's
    participant users, re-apply the policy. A dead headscale host never blocks
    destroying a range."""
    name = "remote_access"

    def run(self, ctx):
        from net.remote_access_ops import teardown_remote_access
        teardown_remote_access(ctx.comp_dir, state=ctx.deployed_state)
        return StepResult(self.name, "ok")
