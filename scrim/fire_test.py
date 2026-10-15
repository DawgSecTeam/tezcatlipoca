import shlex
import time
from pathlib import Path

from common.targets import frozen_roster
from estate import config
from scrim import compworld
from scrim import core
from scrim import procs
from scrim import quotient_api
from scrim.core import log


def write_verify_log(args, label, r):
    """Keep the WHOLE verify output in the run folder, not just the tail the console shows.

    The gate lines scroll past, and the per-config detail behind a FAIL — `'suid-find' present on
    ['team1-web01'] but MISSING on ['team2-web01']`, `pin 'X' absent on every team` — is exactly
    what a post-mortem needs and what the console tail drops. Live 2026-10-15 (run-1c44733b): the
    `misconfig` gate failed at BOTH verifies and the run's evidence held only the one-line summary,
    so the failure could not be root-caused from the artifacts at all.
    """
    run_dir = getattr(args, "run_dir", None)
    if not run_dir:
        return None
    path = Path(run_dir) / "verify.log"
    with path.open("a") as fh:
        fh.write(f"\n===== verify [{label}] rc={r.returncode} {core.now_iso()} =====\n")
        fh.write((r.stdout or "") + "\n=== STDERR ===\n" + (r.stderr or ""))
    core.secure_evidence(path)
    return path


# label matters: this verify runs BEFORE red/blue are staged, and `pins_registered` in particular
# is registered during that staging — the deploy-time result is an INTERIM reading, and the pre-T0
# verify (red_stage, after staging) is the authoritative one. Live 2026-10-15: the same gate read
# FAIL here and PASS three minutes later with nothing else changed, and the report reader has no
# way to tell the two apart unless the run says which is which.
def stage_verify(args, comp, creds, label="interim (pre-staging)"):
    log(f"verify-competition + fire test [{label}]")
    r = procs.run(compworld.verify_cmd(comp, creds), cwd=core.REPO, timeout=1800, check=False)
    print("\n".join((r.stdout or "").splitlines()[-25:]))
    write_verify_log(args, label, r)
    if r.returncode != 0:
        log(f"WARNING: verify reported failures (continuing — investigate before event; "
            f"result is {label}"
            + (" — the pre-T0 verify after staging is the authoritative one" if "interim" in label
               else "") + ")")
    base = core.box_ssh_base(creds)
    web_ip = (frozen_roster(comp) or {}).get("targets", {}) \
        .get("team1-web01", {}).get("ip")
    web_ip = web_ip or f"192.168.{creds['TEAM1_ID']}.4"

    def ssh_web01(cmd, timeout=60):
        return procs.run_tree(base + [f"{creds['BOX_USER']}@{web_ip}", cmd],
                              timeout=timeout, check=False)

    # password on stdin, not an `echo` into the remote shell string: the old form put the
    # password in the local process argv (readable from ps) and a single quote in a
    # generated password would have been a shell syntax error aborting the fire test;
    # same house pattern as beacon_ops._ssh_box
    def sudo_web01(script, timeout=120):
        argv, stdin_text = procs.box_sudo_stdin(base, f"{creds['BOX_USER']}@{web_ip}",
                                                creds["BOX_PW"], script)
        return procs.run_tree(argv, timeout=timeout, check=False, stdin_text=stdin_text)

    # hand the whole fire test to run_fire_test — injectable runners keep it unit-testable
    # offline.
    run_fire_test(args, comp, creds, ssh_web01, sudo_web01)


# a paused engine cannot register a service going down: the harness PAUSES the scoring
# loop at teardown (correct — the event is over) and the loop does not self-resume, so
# the NEXT run's pre-T0 fire test watched a healthy web01 for its whole deadline and
# aborted before T0 (2026-10-08, run 6) — it only appeared to work because red01/loop
# state happened to survive from the run before. Prove the scoring path against a live
# loop: unpause first, with the same two POSTs verify's --fix-round-loop issues.
def _ensure_round_loop(comp, creds):
    base = f"http://{creds['ENGINE_IP']}"
    try:
        import requests
        from verifier.creds import load_admin_password
        pw = creds.get("ADMIN_PW") or load_admin_password(comp, None)
    except Exception as e:                                   # noqa: BLE001
        log(f"fire test: cannot resolve the admin session ({e}) — round loop unchecked")
        return
    try:
        s = requests.Session()
        r = s.post(f"{base}/api/login", json={"username": "admin", "password": pw},
                   timeout=10)
        if r.status_code != 200:
            log(f"fire test: admin login HTTP {r.status_code} — round loop unchecked")
            return
        eng = s.get(f"{base}/api/engine", timeout=10).json()
    except Exception as e:                                   # noqa: BLE001
        log(f"fire test: /api/engine unreadable ({e}) — round loop unchecked")
        return
    if not isinstance(eng, dict) or eng.get("running") is not False:
        return
    try:
        r2 = s.post(f"{base}/api/engine/pause", json={"pause": False}, timeout=10)
        log(f"fire test: scoring loop was PAUSED (left by the previous teardown) — "
            f"unpaused (HTTP {r2.status_code}); the scoreboard advances again")
    except Exception as e:                                   # noqa: BLE001
        log(f"fire test: unpause failed ({e}) — the fire test will likely fail")


# raises (aborting before T0) rather than warning: firing blue into an unproven scoring
# path is how a whole event goes unscored (run-12)
def run_fire_test(args, comp, creds, ssh_web01, sudo_web01):

    _ensure_round_loop(comp, creds)
    units, svc_port, svc_display = compworld._web01_units(comp)
    web_ip = (frozen_roster(comp) or {}).get("targets", {}) \
        .get("team1-web01", {}).get("ip")
    web_ip = web_ip or f"192.168.{creds['TEAM1_ID']}.4"
    # pick the unit that actually exists on this box (apache2 vs httpd across distros)
    probe = ('u=""; for c in %s; do systemctl list-unit-files "$c.service" --no-legend '
             '2>/dev/null | grep -q . && u=$c && break; done; echo "$u"' % " ".join(units))
    r = ssh_web01(probe)
    unit = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else units[0]
    args.web_unit = unit
    log(f"fire test unit: {unit} on {web_ip} (port {svc_port})")
    port = config.active().knobs.scrim_web01_port or svc_port

    # Residue guard: an aborted earlier fire test can leave the unit stopped (scrim-one
    # 2026-10-03: web01's sshd died with the harness and the next attempt inherited the
    # outage until a manual rollback). Restore BEFORE testing — a stop-then-restore cycle
    # that starts from an already-down service proves nothing and heals nothing.
    st = ssh_web01(f"systemctl is-active {shlex.quote(unit)}")
    state = (st.stdout or "").strip().splitlines()[-1] if (st.stdout or "").strip() else "unknown"
    if state != "active":
        log(f"fire test: WARNING {unit} on {web_ip} is '{state}' — a previous aborted run "
            f"left it down; restoring before testing")
        sudo_web01(f"systemctl unmask {shlex.quote(unit)}; "
                   f"systemctl start {shlex.quote(unit)}")
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(15)
            st = ssh_web01(f"systemctl is-active {shlex.quote(unit)}")
            state = ((st.stdout or "").strip().splitlines() or ["unknown"])[-1]
            if state == "active":
                break
        if state != "active":
            raise RuntimeError(
                f"fire test: {unit} on {web_ip} was already down (pre-existing outage) and "
                f"could not be restored. Fix it by hand first — ./mybox web01 'echo $BOX_PW | "
                f"sudo -S systemctl unmask {unit} && sudo systemctl start {unit}' — or "
                f"redeploy-competition.py --boxes web01 --mode rollback-ready.")
        log(f"fire test: pre-existing outage on {unit} restored — proceeding")

    def web01_http():
        r = procs.run_tree(["ssh", "-i", creds["KEY_PATH"], *core.SSH_NOCHECK,
                            f"{creds['VM_USER']}@{creds['ENGINE_IP']}",
                            f"curl -sm 10 -o /dev/null -w '%{{http_code}}' "
                            f"http://192.168.{creds['TEAM1_ID']}.4:{port}/"],
                           timeout=45, check=False)
        return (r.stdout or "").strip()

    # watches the FIRED service's row (<box>-<display>): other scored services may
    # legitimately start down (planted broken services blue must restore), and even a
    # sibling web01 row can be down (scrim-one 2026-10-03: the stale web01-ssh row from
    # an aborted attempt answered "down" for a nginx stop and then refused every nginx
    # restore). Falls back to the first web01 row only when the expected name is not
    # registered at all.
    def scoreboard_down():
        try:
            rows = quotient_api.parsed_status(creds, "team1")
            wanted = f"web01-{svc_display}"
            fired = next((s for s in rows if s["service"] == wanted), None)
            if fired is None:
                fired = next((s for s in rows if s["service"].startswith("web01")), None)
            if fired is None:
                fired = rows[0]
            return not fired["up"], None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    sudo_web01(f"systemctl stop {shlex.quote(unit)}")
    # The scoreboard lags reality: a round that started before the stop lands its
    # result late, and Quotient's per-round cadence means "the latest completed round"
    # can trail the actual service by a minute or two (scrim-one 2026-10-03: the probe
    # read 000 while the scoreboard still said UP at +150s, and the run aborted on a
    # service that was demonstrably down). Poll until BOTH vantages agree the service
    # is down, within a deadline — the engine-path probe is the fast truth, the
    # scoreboard is the one that must eventually agree.
    down = down_err = None
    http_down = ""
    deadline = time.time() + 360
    while time.time() < deadline:
        time.sleep(30)
        down, down_err = scoreboard_down()
        http_down = web01_http()
        if down is True and http_down in ("", "000"):
            break
    log(f"fire test: after stop — scoreboard down={down}"
        + (f" ({down_err})" if down_err else "")
        + f", web01 http={http_down or 'no answer'}")

    restored = healed = False
    for attempt in (1, 2, 3):
        # unmask first: run-12 left the unit unstartable and a plain start was a no-op
        sudo_web01(f"systemctl unmask {shlex.quote(unit)}; "
                   f"systemctl start {shlex.quote(unit)}")
        # same scoreboard-lag deal as the stop phase: poll for agreement, don't sleep
        # a fixed slice and read one stale round
        up = up_err = None
        http_up = ""
        deadline = time.time() + (240 if attempt == 1 else 120)
        while time.time() < deadline:
            time.sleep(30)
            up, up_err = scoreboard_down()
            http_up = web01_http()
            if up is False and http_up not in ("", "000"):
                break
        restored = up is False
        healed = restored and http_up not in ("", "000")
        log(f"fire test: restore attempt {attempt} — scoreboard "
            f"{'up' if up is False else ('unreadable' if up is None else 'still down')}"
            f"{f' ({up_err})' if up_err else ''}, web01 http={http_up or 'no answer'}")
        if healed:
            break
    log(f"fire test: down_detected={down} restored={restored} healed={healed}")
    if not (down and restored and healed):
        raise RuntimeError(
            "fire test failed — team1 web01-http was not verifiably down and restored, so the "
            "scoring path is unproven. Manual fix: ./mybox web01 'echo $BOX_PW | sudo -S "
            f"systemctl unmask {unit} && sudo systemctl start {unit}', confirm "
            f"http://192.168.{creds['TEAM1_ID']}.4/ answers from the engine, then re-run "
            "(the fire test re-validates before T0).")
