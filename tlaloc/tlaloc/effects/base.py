"""The effect registry: ONE description of what an effect is, what it may do,
where it runs, and how to undo it.

Doctrine (the earlier agent kept it in 4-5 places that drifted — prompt prose,
tactic-id sets, a policy elif-chain, the fallback ladder):

* ``tags``     capability + danger class, THE single source:
               access | c2 | pressure | persistence | endgame
               plus passive | impacty | unrecoverable.
               ("unrecoverable" replaces the earlier agent's three hardcoded mode
               lists.)
* ``check()``  per-effect precondition (needs root, needs C2, ladder window).
               Raise PolicyError to reject a candidate; raise GuardrailError
               only if something unsafe was attempted.
* ``run()``    the body. Effects do NOT call gate primitives — the shared
               execute() template runs the kill switch, ensure_target,
               free-form lint, check(), the dry-run short-circuit, and the
               bookkeeping. An effect cannot forget a gate.
* Attribution is code-side: an effect that took a scored service down sets
  ``Result.impact``; the ledger never depends on a caller remembering a
  ``service=`` parameter (the earlier agent's honor-system audited defect, twice).
"""

import shlex
from dataclasses import dataclass, field

from ..errors import EffectParamError

ALL_TAGS = {"access", "c2", "pressure", "persistence", "endgame",
            "passive", "impacty", "unrecoverable"}


@dataclass
class Result:
    ok: bool
    detail: str = ""
    data: dict = field(default_factory=dict)
    impact: dict | None = None   # {"service","mode"} -> impact ledger (template adds)
    restore: dict | None = None  # {"recipe": str|list, "secrets"?: str} -> restore table
    artifacts: list = field(default_factory=list)   # add_artifact kwargs
    # Set when the box was MUTATED even though the objective was not met (a C2
    # implant that installed but never checked in). The template records the
    # artifact and the undo recipe from this flag, never from ok — a record
    # gated on a channel proof disappears exactly when it is most needed.
    installed: bool | None = None


class Effect:
    id = ""
    name = ""
    description = ""
    # param spec: {name: {type: str|int|enum|list, required, choices, freeform,
    #                     max, desc}}. "freeform": True marks LLM-authored
    # shell text — the gate lints it with the target-OS pattern set.
    params = {}
    tags = frozenset()
    # declarative preconditions read by check() default: any foothold / root
    # path / c2 availability / os ("linux"|"windows"|"any")
    needs = {"foothold": None, "root": False, "c2": False, "os": "any"}
    # param name that scopes the retry budget; None is per box
    failure_scope = None

    def check(self, ctx, params):
        """Default declarative preconditions; override and call super()."""
        from ..errors import PolicyError
        needs = self.needs or {}
        ip = params.get("ip")
        if needs.get("os") and needs["os"] != "any" and ip:
            if ctx.os_of(ip) != needs["os"]:
                raise PolicyError(f"{self.id} requires a {needs['os']} box")
        if (needs.get("foothold") or needs.get("root")) and ip:
            fh = ctx.world.foothold(ip)
            if fh is None:
                raise PolicyError(f"{self.id} needs a foothold on {ip}")
            if needs.get("root") and not can_root(fh):
                raise PolicyError(f"{self.id} needs a root path on {ip}")
        if needs.get("c2") and ctx.c2 is None:
            raise PolicyError(f"{self.id} needs the C2 layer (c2.enabled)")

    def run(self, ctx, params):
        raise NotImplementedError


# --- the routing predicate every root-capable path shares ----------------------

ESCALATIONS = ("sudo", "sudo_pw", "escape:", "capexec:")


def can_root(fh):
    if not fh:
        return False
    if fh.get("kind") == "root" or fh.get("root_capable"):
        return True
    esc = fh.get("escalation") or ""
    return any(esc.startswith(e) for e in ESCALATIONS)


def root_cmd(cmd, fh):
    """Wrap a command for the foothold's escalation label. Escape wrappers
    break on arbitrary quoting — free-form commands must not ride them (the
    improvise effect runs free-form unprivileged or via sudo, per label)."""
    if fh.get("windows"):
        return cmd  # SYSTEM label is not routable; admin rides the session
    # A multi-line shell script cannot ride a bare prefix (`sudo -n if ...` is a
    # syntax error, not a script): give it one root shell and one quoted argument.
    if "\n" in cmd:
        cmd = f"sh -c {shlex.quote(cmd)}"
    esc = fh.get("escalation") or ""
    if esc == "sudo":
        return f"sudo -n {cmd}"
    if esc == "sudo_pw":
        pw = (fh.get("password") or "").replace("'", "'\\''")
        return f"echo '{pw}' | sudo -S -p '' {cmd}"
    if esc.startswith("escape:") or esc.startswith("capexec:"):
        wrapper = esc.split(":", 1)[1]
        return f"{wrapper} {cmd}"
    if fh.get("kind") == "root":
        return cmd
    return f"sudo -n {cmd}"


REGISTRY = {}


def register(cls):
    effect = cls()
    if not effect.id:
        raise ValueError(f"{cls.__name__} has no id")
    unknown = set(effect.tags) - ALL_TAGS
    if unknown:
        raise ValueError(f"{effect.id}: unknown tags {sorted(unknown)}")
    REGISTRY[effect.id] = effect
    return cls


def get(effect_id):
    try:
        return REGISTRY[effect_id]
    except KeyError:
        raise EffectParamError(f"unknown effect {effect_id!r}") from None


def catalog():
    """Machine-readable registry dump (status command, arbiter prompt)."""
    return [{"id": e.id, "name": e.name, "description": e.description,
             "tags": sorted(e.tags), "params": e.params}
            for e in sorted(REGISTRY.values(), key=lambda e: e.id)]


def validate_params(effect, params):
    """Shape validation only (content safety is the gate's job). Unknown or
    missing-required params are EffectParamError — a malformed decision is a
    bug upstream, not something to execute partially."""
    params = dict(params or {})
    clean = {}
    for name, spec in effect.params.items():
        value = params.pop(name, None)
        required = bool(spec.get("required"))
        if value is None or value == "":
            if required:
                raise EffectParamError(f"{effect.id}: missing param {name!r}")
            continue
        kind = spec.get("type", "str")
        if kind == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise EffectParamError(
                    f"{effect.id}: param {name!r} must be int") from None
        elif kind == "enum":
            if value not in spec.get("choices", []):
                raise EffectParamError(
                    f"{effect.id}: param {name!r}={value!r} not in "
                    f"{spec['choices']}")
        elif kind == "list":
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or any(not isinstance(i, str) for i in value):
                raise EffectParamError(
                    f"{effect.id}: param {name!r} must be a list of strings")
            value = list(value)
        else:
            value = str(value)
        max_len = spec.get("max")
        if max_len and isinstance(value, str) and len(value) > max_len:
            raise EffectParamError(
                f"{effect.id}: param {name!r} exceeds {max_len} chars")
        clean[name] = value
    if params:
        raise EffectParamError(f"{effect.id}: unknown params {sorted(params)}")
    return clean
