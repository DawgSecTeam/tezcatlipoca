import json
import os
import shlex
import time

from scrim import compworld
from scrim import core
from scrim import procs
from scrim import quotient_api
from scrim.core import log


def stage_verify(args, comp, creds):
    log("verify-competition + fire test")
    r = procs.run(compworld.verify_cmd(comp, creds), cwd=core.REPO, timeout=1800, check=False)
    print("\n".join((r.stdout or "").splitlines()[-25:]))
    if r.returncode != 0:
        log("WARNING: verify reported failures (continuing — investigate before event)")
    base = core.box_ssh_base(creds)
    web_ip = (json.loads((comp / "targets.json").read_text()).get("targets", {})
              .get("team1-web01", {}).get("ip"))
    web_ip = web_ip or f"192.168.{creds['TEAM1_ID']}.4"

    def ssh_web01(cmd, timeout=60):
        return procs.run_tree(base + [f"{creds['BOX_USER']}@{web_ip}", cmd],
                              timeout=timeout, check=False)

    def sudo_web01(script, timeout=120):
        """Run `script` as root on web01 with the box password on STDIN.

        The old call piped the box password through an `echo` into `sudo -S` inside a
        remote shell string built with %-interpolation: the password landed in the local
        process argv (readable from ps on the operator host) and a single quote in a
        generated password would have been a shell syntax error that aborted the fire
        test. beacon_ops._ssh_box already established the house pattern — `sudo -S` reads
        the password from stdin and the script follows it on the same stream — so reuse it.
        """
        argv, stdin_text = procs.box_sudo_stdin(base, f"{creds['BOX_USER']}@{web_ip}",
                                                creds["BOX_PW"], script)
        return procs.run_tree(argv, timeout=timeout, check=False, stdin_text=stdin_text)

    # hand the whole fire test to run_fire_test — injectable runners keep it unit-testable
    # offline.
    run_fire_test(args, comp, creds, ssh_web01, sudo_web01)


def _ensure_round_loop(comp, creds):
    """A paused engine cannot register a service going down.

    The harness PAUSES the scoring loop at teardown (correct — the event is over)
    and the loop does not self-resume, so the NEXT run's pre-T0 fire test watched a
    healthy web01 for its whole deadline, saw no transition, and aborted before T0
    (2026-10-08, run 6). It only appeared to work before because red01/loop state
    happened to survive from the run before. Prove the scoring path against a live
    loop: unpause first, with the same two POSTs verify's --fix-round-loop issues.
    """
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


def run_fire_test(args, comp, creds, ssh_web01, sudo_web01):

    """Prove the scoring path end to end: stop web01's scored unit, watch the scoreboard
    register it down, restore it, watch the scoreboard heal. Raises (aborting before T0)
    rather than warning: firing blue into an unproven scoring path is how a whole event
    goes unscored (run-12).

    `ssh_web01(cmd)` runs a shell command as the box user, `sudo_web01(script)` as root —
    injected so tests can script the box without SSH."""
    _ensure_round_loop(comp, creds)
    units, svc_port, svc_display = compworld._web01_units(comp)
    web_ip = (json.loads((comp / "targets.json").read_text()).get("targets", {})
              .get("team1-web01", {}).get("ip"))
    web_ip = web_ip or f"192.168.{creds['TEAM1_ID']}.4"
    # pick the unit that actually exists on this box (apache2 vs httpd across distros)
    probe = ('u=""; for c in %s; do systemctl list-unit-files "$c.service" --no-legend '
             '2>/dev/null | grep -q . && u=$c && break; done; echo "$u"' % " ".join(units))
    r = ssh_web01(probe)
    unit = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else units[0]
    args.web_unit = unit
    log(f"fire test unit: {unit} on {web_ip} (port {svc_port})")
    port = os.environ.get("SCRIM_WEB01_PORT", str(svc_port))

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
        """HTTP code for web01 over the engine's network path ('' / 000 = no answer)."""
        r = procs.run_tree(["ssh", "-i", creds["KEY_PATH"], *core.SSH_NOCHECK,
                            f"{creds['VM_USER']}@{creds['ENGINE_IP']}",
                            f"curl -sm 10 -o /dev/null -w '%{{http_code}}' "
                            f"http://192.168.{creds['TEAM1_ID']}.4:{port}/"],
                           timeout=45, check=False)
        return (r.stdout or "").strip()

    def scoreboard_down():
        """(down, err): down=True/False; err set when the scoreboard itself is unreadable.
        Watches the FIRED service's row (<box>-<display>) — other scored services may
        legitimately start down (planted broken services blue must restore), and even a
        sibling web01 row can be down (scrim-one 2026-10-03: the stale web01-ssh row
        from an aborted attempt answered "down" for a nginx stop and then refused every
        nginx restore). Falls back to the first web01 row only when the expected name
        is not registered at all."""
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
