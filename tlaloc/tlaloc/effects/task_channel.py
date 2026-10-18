"""task_channel — run one command through the C2 channel.

Why this is an effect and not a bypass: the channel does not need SSH, so it is
red's path when blue closes the login, and it is the only path to a Windows box
whose Administrator login IS the SSH account (lockout/burndown depend on it). It
still passes the gate, still obeys the pressure caps, and its impact
attribution does not depend on a caller remembering a `service=` argument.

Attribution: the command's intent is classified (`decide.classify_command`, clef
with a deterministic floor) and an intent of `takedown` records an impact row for
the scored service named in `service`. That parameter is the *advertising claim*
— nothing is attributed unless the task reported success AND the classifier
agrees it was an impact move, which is what the earlier agent's substring sniff
got wrong in both directions.
"""

from ..errors import PolicyError
from .base import Effect, Result, register


@register
class TaskChannel(Effect):
    id = "task_channel"
    name = "run a command over C2"
    description = ("Execute one command on a box through the C2 channel (no SSH). Use "
                   "it when the login path is closed, when blue kills SSH, or when the "
                   "box is a Windows host whose Administrator account is the SSH path. "
                   "Pass service= when the command takes a scored service down so the "
                   "impact ledger is right.")
    params = {
        "ip": {"type": "str", "required": True, "desc": "target box"},
        "command": {"type": "str", "required": True, "freeform": True, "max": 200,
                    "desc": "the command to run through the channel"},
        "box_id": {"type": "str", "desc": "manifest/callback key (default <box>-<team>)"},
        "service": {"type": "str",
                    "desc": "the scored service this command targets (for the ledger)"},
        "goal": {"type": "str", "desc": "one line of intent, for the report"},
    }
    tags = frozenset({"c2", "impacty"})
    needs = {"foothold": False, "root": False, "c2": True, "os": "any"}

    def check(self, ctx, params):
        super().check(ctx, params)
        if ctx.c2 is None:
            raise PolicyError("task_channel needs the C2 layer (c2.enabled)")
        ip = params["ip"]
        if not self._has_channel(ctx, ip, params.get("box_id")):
            raise PolicyError(f"no live callback for {ip} — nothing to task")

    def _has_channel(self, ctx, ip, box_id=None):
        from .implant_channel import box_id_for
        key = box_id_for(ctx, ip, box_id)
        try:
            callbacks = ctx.c2.callbacks()
        except Exception:
            return False
        record = callbacks.get(key) or callbacks.get(ip)
        if not record:
            return False
        max_age = float(ctx.cfg["c2"].get("callback_max_age_sec", 120))
        seen = record.get("seen_at")
        if not seen:
            return False
        age = ctx.now() - float(seen)
        return age <= max_age

    def run(self, ctx, params):
        from .implant_channel import box_id_for
        ip = params["ip"]
        command = params["command"]
        box_id = box_id_for(ctx, ip, params.get("box_id"))
        timeout = int(ctx.cfg["c2"].get("task_timeout_sec", 120))
        try:
            outcome = ctx.c2.task(box_id, command, timeout=timeout)
        except Exception as exc:
            return Result(False, f"c2 tasking failed: {type(exc).__name__}: {exc}")

        ok = bool(outcome.get("ok"))
        output = str(outcome.get("output") or "")[:2048]
        intent = None
        decider = getattr(ctx, "decider", None)
        if ok and decider is not None:
            try:
                decision = decider.classify_command(command, ctx.os_of(ip))
                intent = decision.answer
                if intent == "takedown":
                    ctx.log.emit("command_intent", command=command[:120],
                                 intent=intent, source=decision.source,
                                 top_prob=round(decision.top_prob, 3))
            except Exception as exc:          # a classifier fault must not fail the task
                ctx.log.emit("command_intent_error", error=str(exc))

        result = Result(ok, (f"{params.get('goal') or command[:60]}: "
                             f"{outcome.get('detail') or 'task completed'}"),
                        data={"task_id": outcome.get("task_id"), "output": output,
                              "intent": intent, "box_id": box_id},
                        # A task that RAN touched the box, whatever the objective
                        # did: record the undo from the run, never gate it on the
                        # task's success (a failed task can still have stopped a
                        # service on its way out).
                        installed=bool(outcome.get("task_id")))
        if ok and intent == "takedown" and params.get("service"):
            result.impact = {"ip": ip, "service": params["service"], "mode": "c2_task",
                             "detail": f"via C2: {command[:120]}"}
        # Every mutation carries its undo (AGENTS.md invariant 9). A C2-tasked
        # command is not derivable from the box the way a takedown's MOVE is, so
        # the recipe names the exact text that ran and, when the command is a
        # classified takedown of a named service, the one step that undoes it.
        if outcome.get("task_id"):
            result.restore = {"recipe": _undo_recipe(ctx, ip, command, params, ok, intent),
                              "secrets": None}
        return result


def _undo_recipe(ctx, ip, command, params, ok, intent):
    """The undo steps for one C2-tasked command."""
    steps = []
    service = str(params.get("service") or "").strip()
    if service and (intent == "takedown" or not ok):
        # The service may be a unit name or a pin label; the operator's own
        # table is where the exact unit is resolved.
        unit = service if service.endswith(".service") else f"{service}.service"
        steps.append(f"Start-Service '{service}'" if ctx.is_windows(ip)
                     else f"systemctl start {unit}")
    steps.append(f"# C2 task ran on {ip}: {str(command)[:180]}")
    steps.append("# anything else it changed is not derivable from here — "
                 "reverse by hand")
    return steps
