"""persist — plant one persistence family; names are drawn from artifactnames (never literals) and the record comes from the install, never a check-in."""

from ..errors import PolicyError
from . import artifactnames as names_mod
from .base import Effect, Result, can_root, register

# `family="auto"` walks this order and takes the first family not already standing.
AUTO_ORDER_LINUX = ("systemd_unit", "profile", "sshd_dropin", "cron", "cron_reaper",
                    "timer", "user_cron", "keys", "suid", "preload", "immu",
                    "timestomp", "logwipe", "decoy")
AUTO_ORDER_WINDOWS = ("scheduled_task", "defrun", "service", "wmisub", "hide",
                      "logwipe", "timestomp", "decoy")


def _handler(windows):
    """Lazy import keeps the registry loadable; a handler module missing from the build returns None — test_effects guards catalogue completeness."""
    try:
        if windows:
            from . import persist_windows as handler
        else:
            from . import persist_linux as handler
    except ImportError:
        return None
    return handler


def families(windows=False):
    handler = _handler(windows)
    return dict(handler.FAMILIES) if handler is not None else {}


def auto_order(windows=False):
    known = families(windows)
    order = AUTO_ORDER_WINDOWS if windows else AUTO_ORDER_LINUX
    return [f for f in order if f in known] + [f for f in known if f not in order]


def standing_families(world, ip):
    return {a.get("family") or a.get("kind") for a in world.artifacts(ip)}


@register
class Persist(Effect):
    id = "persist"
    name = "plant persistence (one family)"
    description = ("Install one persistence/backdoor family on a box where we have a "
                   "root/admin path, and record what was planted. family=auto picks "
                   "the first family not already standing on this box, so the same "
                   "effect both seeds day-0 access and re-plants after blue cleans. "
                   "Every family is blue-recoverable and carries its removal recipe.")
    params = {
        "ip": {"type": "str", "required": True, "desc": "target box"},
        "family": {"type": "str", "desc": "family name, or 'auto' (default)"},
        "force": {"type": "str", "desc": "'true' to re-plant a standing family"},
    }
    tags = frozenset({"persistence"})
    needs = {"foothold": True, "root": False, "c2": False, "os": "any"}

    def check(self, ctx, params):
        super().check(ctx, params)
        ip = params["ip"]
        fh = ctx.world.foothold(ip)
        if not (can_root(fh) or str((fh or {}).get("user", "")).lower()
                in ("administrator", "admin")):
            raise PolicyError(f"persist needs a root/admin path on {ip} — escalate first")
        family = params.get("family") or "auto"
        if family != "auto":
            known = families(ctx.is_windows(ip))
            if family not in known:
                raise PolicyError(f"unknown persistence family {family!r} for "
                                  f"{'windows' if ctx.is_windows(ip) else 'linux'}")

    def run(self, ctx, params):
        ip = params["ip"]
        windows = ctx.is_windows(ip)
        handler = _handler(windows)
        if handler is None:
            return Result(False, f"persist: the {'windows' if windows else 'linux'} "
                                 "handler module is not built in this deployment")
        known = families(windows)
        standing = standing_families(ctx.world, ip)
        force = str(params.get("force", "")).lower() == "true"

        family = params.get("family") or "auto"
        if family == "auto":
            family = next((f for f in auto_order(windows) if f not in standing), None)
            if family is None:
                return Result(False, f"every persistence family is already standing "
                                     f"on {ip} ({sorted(standing)}) — re-plant with "
                                     f"family=<name> force=true")
        elif family in standing and not force:
            return Result(False, f"{family} is already standing on {ip} (force=true "
                                 "to re-plant, which draws fresh names)")

        mode = names_mod.mode_from_cfg(ctx.cfg)
        if windows:
            names = names_mod.windows_names(ctx.world, rng=ctx.rng, mode=mode)
        else:
            names = names_mod.linux_names(ctx.world, rng=ctx.rng, mode=mode)

        result = handler.apply(ctx, ip, family, names)
        if result is None:
            return Result(False, f"{family}: handler returned nothing")
        result.data.setdefault("family", family)
        result.data.setdefault("names", names)
        if result.restore is not None:
            result.restore.setdefault("recipe", [])
            if isinstance(result.restore["recipe"], str):
                result.restore["recipe"] = [result.restore["recipe"]]
            result.restore["recipe"] = list(result.restore["recipe"]) + [
                f"# planted by persist family={family} mode={mode}"]
        for artifact in result.artifacts:
            artifact.setdefault("family", family)
        return result
