"""The shared execute() template — the single path from decision to action.

Order (and it is not negotiable, because every bypass in the earlier agent was
an exception to this order): kill switch -> param shape -> ensure_target -> free-form lint ->
effect precondition -> dry-run short-circuit -> run -> bookkeeping -> audit.

GuardrailError propagates (the loop stops; the design was violated).
PolicyError becomes a failed result (the candidate was rejected; the loop
re-plans). Everything else becomes ok=False — an effect that dies does not
take the event with it.
"""

from .base import (ALL_TAGS, ESCALATIONS, REGISTRY, Effect, Result, can_root, catalog,
                   get, register, root_cmd, validate_params)
from ..errors import EffectParamError, GuardrailError, PolicyError

__all__ = ["REGISTRY", "Result", "Effect", "execute", "get", "register",
           "catalog", "can_root", "root_cmd", "validate_params",
           "ALL_TAGS", "ESCALATIONS", "reload_registry", "MODULES", "MISSING_MODULES"]

# The complete effect catalogue, in registry order, as file stems. tests/
# test_registry.py asserts MISSING_MODULES is empty: a module that is not
# written yet is skipped here so a phase can be tested in isolation, but the
# suite refuses to call the catalogue complete until every name exists.
MODULES = ("recon", "spray_access", "reestablish", "escalate",
           "takedown", "block_port", "rotate_credlist", "lockout",
           "persist", "implant_channel", "task_channel", "improvise")

MISSING_MODULES = []


def execute(effect_id, params, ctx, *, source="deterministic", cycle=None):
    """Run one effect through every invariant. Returns Result."""
    effect = get(effect_id)
    started = ctx.now()
    params = validate_params(effect, params)
    ip = params.get("ip")
    try:
        ctx.gate.check_kill_switch()
        if ip:
            ctx.gate.ensure_target(ip)
        # Free-form (LLM-authored) text rides the per-OS lint + caps here —
        # every effect, including C2 tasking and improvisation.
        for name, spec in effect.params.items():
            if spec.get("freeform") and params.get(name):
                ctx.gate.ensure_command(params[name], ctx.os_of(ip or "") or "linux")
        effect.check(ctx, params)
        if ctx.dry_run:
            result = Result(ok=True, detail=f"[dry-run] {effect_id}",
                            data={"would": {k: str(v) for k, v in params.items()}})
        else:
            result = effect.run(ctx, params)
            if result is None:
                result = Result(ok=False, detail=f"{effect_id} returned no result")
    except GuardrailError:
        raise                       # hard safety: never a failed-continue
    except PolicyError as exc:
        ctx.world.tally_reject(effect_id, ctx.intel.team_of(ip) if ip else None,
                               str(exc))
        result = Result(ok=False, detail=f"precondition: {exc}")
    except EffectParamError:
        raise
    except Exception as exc:        # transport blowups, box weirdness
        result = Result(ok=False, detail=f"{type(exc).__name__}: {exc}")

    mutated = bool(result.ok or result.installed)
    if mutated and not ctx.dry_run:
        if result.ok and result.impact:
            ctx.world.add_impact(result.impact.get("ip") or ip or "",
                                 result.impact.get("service", "?"),
                                 result.impact.get("mode", effect_id),
                                 result.impact.get("detail"))
        for artifact in result.artifacts:
            ctx.world.add_artifact(**artifact)
        if result.restore is not None and (result.ok or result.installed):
            ctx.world.add_restore(effect_id, ip or "",
                                  (result.impact or {}).get("service", ""),
                                  result.restore.get("recipe", ""),
                                  result.restore.get("secrets"))
    if result.ok and "unrecoverable" in effect.tags:
        result.data["unrecoverable"] = True
    if not result.ok:
        ctx.world.tally_failure(effect_id, ip)

    ctx.log.action(cycle=cycle, effect=effect_id, params=params,
                   result=result, source=source, duration_s=ctx.now() - started)
    return result


def reload_registry():
    """Import every effects module for its registration side effect.

    A module whose FILE does not exist yet is recorded in MISSING_MODULES and
    skipped (phased migration); a module that exists but fails to import
    propagates — a broken effect module is never silently absent.
    """
    from importlib import import_module
    from pathlib import Path

    MISSING_MODULES.clear()
    here = Path(__file__).parent
    for name in MODULES:
        if not (here / f"{name}.py").exists():
            MISSING_MODULES.append(name)
            continue
        import_module(f"{__name__}.{name}")
