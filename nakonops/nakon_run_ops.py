"""Run nakon deploy on the scoring engine and parse its structured result."""

import json
import os
import re
import shlex
import subprocess
import threading
import time

from common.constants import NAKON_DIR
from common.ssh_ops import _engine_opts
from nakonops.nakon_bundle_ops import build_nakon_bundle
from pathlib import Path


class NakonResult:
# Structured outcome of one `nakon deploy` (M4 plant-coverage source).
#
#     failed: raw FAILED output lines (the human-facing tally, as before).
#     machines: per-machine step results from nakon deploy --json — name, exit_status,
#     error, and steps[{name, rc, ...}] — so coverage = expected configs minus rc!=0
#     steps, exactly, instead of parsed from interleaved --jobs stdout.

    def __init__(self, failed, machines):
        self.failed = failed
        self.machines = machines or []

    def __bool__(self):
        return not self.failed

    def failed_configs(self):
    # {machine_name: set(config names whose step rc != 0 or never reported)}.
        out = {}
        for m in self.machines:
            bad = {s["name"] for s in m.get("steps", []) if s.get("rc") != 0}
            if m.get("error") and not m.get("steps"):
                bad = {"<machine failed before any step>"}
            if bad:
                out[m.get("name") or "?"] = bad
        return out


def _nothing_answered(machines):
# Why a rc=0 plant applied nothing, or None when the run was real.
#
#     `machines` is nakon deploy --json's per-machine outcome list. A machine that was
#     never reached carries no steps at all (`steps: []`) — nakon records the connection
#     error in `error` and moves on, so an all-unreachable plant is indistinguishable from
#     a clean one by exit code alone.
#
#     Deliberately narrow. It fires only when NO machine produced a single step result:
#     a plant where even one host ran one step is a real (if disappointing) outcome and
#     must not be escalated, or the floor would abort runs that are merely imperfect —
#     exactly the tolerated-failure case the ledger exists for.
    if not machines:
        return "no per-machine results were reported"
    ran = [m for m in machines if m.get("steps")]
    if ran:
        return None
    names = ", ".join(str(m.get("name") or m.get("ip") or "?") for m in machines[:6])
    return f"all {len(machines)} machine(s) ran zero steps ({names})"


def _machines_from_line(line):
# Parse a nakon result line into its machine list, tolerating a glued status line and a
# "[nakon] " prefix. Returns None when the line carries no result object.
    if '"machines"' not in line:
        return None
    idx = line.find("{")
    if idx < 0:
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(line[idx:])
    except ValueError:
        return None
    machines = parsed.get("machines")
    return machines if isinstance(machines, list) else None


def run_nakon(key, scoring_user, scoring_ip, bundle, config_path, only=None, timeout=2400,
              strict=True, jobs=1, run_tag=None):
# Push the bundle to the scoring engine and run `nakon deploy` there.
#
#     Returns the FAILED step lines from the deploy output ([] when the plant was
#     clean, or when --strict aborted on the first one). jobs > 1 passes nakon's
#     --jobs through: per-machine work is atomic in nakon's runner, so machines
#     plant in parallel while each machine's steps keep their order.
#
#     Staging is per-run (M2.3): /tmp/nakon-<tag> on the way up, /opt/nakon/<tag> on
#     the engine, created and removed only by the run that owns them. Concurrent
#     nakon runs (M2.4's parallel domain passes) no longer race on a shared staging
#     slot.
    ssh_base = [
        "ssh", "-i", str(key), *_engine_opts(host=scoring_ip),
        f"{scoring_user}@{scoring_ip}",
    ]

    if run_tag is None:
        run_tag = f"{Path(config_path).stem}-{time.strftime('%Y%m%d-%H%M%S')}"
    run_tag = re.sub(r"[^A-Za-z0-9._-]", "_", run_tag)
    tmp_dir = f"/tmp/nakon-{run_tag}"
    opt_dir = f"/opt/nakon/{run_tag}"

    subprocess.run(ssh_base + [f"rm -rf {tmp_dir} && mkdir -p {tmp_dir}"],
                   check=True, timeout=60)

    subprocess.run(
        [
            "scp", "-i", str(key), *_engine_opts(host=scoring_ip),
            "-r",
            str(NAKON_DIR / "nakon"),
            str(bundle),
            str(Path(config_path).resolve()),
            f"{scoring_user}@{scoring_ip}:{tmp_dir}/",
        ],
        check=True, timeout=600,
    )

    remote_config = f"{opt_dir}/{Path(config_path).name}"
    only_args = ""
    if only:
        only_args = " --only " + " ".join(shlex.quote(name) for name in only)
    strict_arg = " --strict" if strict else ""
    jobs_arg = f" --jobs {int(jobs)}" if int(jobs) > 1 else ""

    setup_cmd = (
        f"sudo mkdir -p {opt_dir} && "
        f"sudo cp -r {tmp_dir}/. {opt_dir}/ && "
        # paramiko is needed by the remote nakon (root's python3). Check before install:
        # a blind install on every deploy races concurrent runs on the engine's pip.
        "{ sudo python3 -c 'import paramiko' 2>/dev/null || "
        "sudo pip3 install --break-system-packages paramiko; }"
    )
    deploy_cmd = (
        f"cd {opt_dir} && sudo python3 -m nakon deploy "
        f"--bundle {opt_dir}/{bundle.name} --config {remote_config}{only_args}{strict_arg}{jobs_arg}"
        " --json"
    )

    try:
        subprocess.run(ssh_base + [setup_cmd], check=True, timeout=300)
        failed_steps = []
        machines_json = None
        # Long plants (minutes, over the operator's LAN) sometimes lose the outer
        # ssh session outright — rc=255 with no FAILED step is a transport death,
        # not a plant verdict. Re-run the bundle: steps are idempotent re-plants.
        for attempt in range(1, 4):
            proc = subprocess.Popen(ssh_base + [deploy_cmd], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)

            def _pump():
                nonlocal machines_json
                for line in proc.stdout:
                    print(line, end="")
                    if "FAILED" in line:
                        failed_steps.append(line.strip())
                    # nakon deploy --json prints the structured outcomes as one JSON
                    # object with a "machines" key. It is USUALLY the final line, but
                    # nakon's status line can land without a trailing newline, gluing the
                    # JSON onto it ("...exit 0.{...}") — and it carries a "[nakon] " prefix
                    # on some builds. Requiring the line to START with "{" then silently
                    # drops the whole result set, and the rc=0 "nothing answered" guard
                    # below aborts a fully-green pass (live-found 2026-10-13 on
                    # crimson-relay phase 7). Decode from the first "{" instead.
                    parsed = _machines_from_line(line)
                    if parsed is not None:
                        machines_json = parsed

            pump = threading.Thread(target=_pump, daemon=True)
            pump.start()
            try:
                proc.wait(timeout=timeout)
            finally:
                pump.join(timeout=5)
            if proc.returncode == 0:
                # rc=0 is not the same as "the plant did something". A strict=False run
                # whose every machine was unreachable finishes rc=0 with zero FAILED
                # steps, because a host that never answered cannot report a failing
                # step. That is how the scale8 soak's repair sweep "succeeded" against
                # 32 machines that did not exist (2026-10-02) and checkpointed phase 5.
                # 2 flaky steps of 41 is a tolerated failure; 41 of 41 is a broken
                # sweep, and only the latter must stop the run.
                nothing = _nothing_answered(machines_json)
                if nothing:
                    raise RuntimeError(
                        f"nakon deploy reported success but nothing was applied: "
                        f"{nothing}. rc=0 with no per-machine step results means the "
                        f"hosts never answered (or --only selected no machine) — "
                        f"treating that as a completed plant is what let the scale8 "
                        f"soak's repair sweep 'succeed' against machines that did not "
                        f"exist. Refusing to continue; check that the boxes are up and "
                        f"reachable, then re-run."
                    )
                return NakonResult(failed_steps, machines_json)
            if failed_steps or attempt == 3:
                break
            print(f"\n  nakon ssh session died (rc=255, no FAILED steps) — "
                  f"retry {attempt}/3 in 20s...")
            failed_steps = []
            machines_json = None
            time.sleep(20)
        if failed_steps:
            print(f"\n  Nakon plant FAILED steps: {len(failed_steps)}")
            for line in failed_steps[:10]:
                print(f"    {line[:180]}")
        _preserve_failed_run(ssh_base, opt_dir, failed_steps)
        raise RuntimeError(
            f"nakon deploy failed rc={proc.returncode}"
            + (f" ({len(failed_steps)} FAILED steps above)" if failed_steps else ""))
    except subprocess.TimeoutExpired:
        try:
            # Scoped to THIS run's staging path: the remote cmdline carries
            # /opt/nakon/<tag>/, so a concurrent run's deploy (its own tag) survives.
            subprocess.run(ssh_base + [f"sudo pkill -9 -f '{opt_dir}' || true"],
                           timeout=30)
        except Exception:
            pass
        raise
    finally:
        # This run owns its staging dirs; only it removes them.
        try:
            subprocess.run(ssh_base + [f"rm -rf {tmp_dir} && sudo rm -rf {opt_dir}"],
                           check=False, timeout=60)
        except Exception:
            pass


def _preserve_failed_run(ssh_base, opt_dir, failed_steps, runner=None):
# Move a failed plant's staging (its run logs included) aside, then print
#     the failed steps' output tails.
#
#     The per-machine .log under <bundle>/runs/<stamp>/ is the ONLY record of WHY
#     a step failed, and run_nakon's finally deletes the whole staging dir — so
#     the first IIS FTP golden failure (2026-10-11) printed a `logs:` path that
#     stopped existing seconds later and was undiagnosable without a re-run.
#     Failed runs keep their evidence at /opt/nakon/failed-<tag>-<ts>; successful
#     runs are still cleaned up normally.
    if not failed_steps:
        return None
    run = runner or (lambda argv, timeout=90:
                     subprocess.run(argv, capture_output=True, text=True,
                                    timeout=timeout))
    keep = (f"/opt/nakon/failed-{opt_dir.rsplit('/', 1)[-1]}"
            f"-{time.strftime('%Y%m%d-%H%M%S')}")
    moved = run(ssh_base + [f"sudo mv {opt_dir} {keep} && "
                            f"sudo chmod -R a+rX {keep}; test -d {keep}"])
    if moved.returncode != 0:
        print("  (failed-run logs could not be preserved — staging already gone)")
        return None
    print(f"  failed-run logs preserved on the engine at {keep}")
    machines = []
    for line in failed_steps:
        m = re.search(r"\[([-\w.]+)\].*?(?:FAILED|failed)", line)
        if m and m.group(1) not in machines:
            machines.append(m.group(1))
    for machine in machines[:5]:
        tail = run(ssh_base + [
            f"for f in {keep}/*/runs/*/{machine}.log {keep}/runs/*/{machine}.log; do "
            f'[ -f "$f" ] && echo "--- $f" && tail -n 50 "$f"; done | head -120'])
        text = (tail.stdout or "").strip()
        print(text[:4000] if text else
              f"  (no per-machine log for {machine} under {keep})")
    return keep


def _run_single_nakon_config(machine, configurations, key, scoring_user, scoring_ip, comp_dir,
                              tag, timeout=1800, strict=True):
# Deploy one machine with overridden configs in isolation (reboot-safe).
#
#     The config file doubles as a done-marker (deploy_domain_configs skips ADDS
#     promotion when .nakon-domain-<team>-adds.json exists), so it is written as
#     .pending and renamed into place only after the run succeeds — a failed
#     promotion must not read as done on the next resume.
    tmp_machine = {**machine, "configurations": configurations}
    final_path = comp_dir / f".nakon-domain-{tag}.json"
    pending_path = comp_dir / f".nakon-domain-{tag}.pending.json"
    pending_path.write_text(json.dumps({"machines": [tmp_machine]}, indent=2))
    os.chmod(pending_path, 0o600)
    bundle = build_nakon_bundle(pending_path)
    try:
        run_nakon(key, scoring_user, scoring_ip, bundle, pending_path,
                  only=[machine["name"]], timeout=timeout, strict=strict, run_tag=tag)
    except BaseException:
        pending_path.unlink(missing_ok=True)
        raise
    os.replace(pending_path, final_path)
