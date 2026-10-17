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


# Seeing a step line proves the plant started executing — after that, a dead ssh session leaves
# the outcome unknown.
_STEP_LINE_RE = re.compile(r"\bstep \d+/\d+\b")

# A transport death in the first seconds is a cold ssh (auth, engine handshake, staging) —
# nothing ran and the 20s retry is safe. Past this window steps were in flight.
COLD_DEATH_WINDOW = 120


class NakonResult:
    # Structured outcome of one `nakon deploy`: `failed` is the raw FAILED lines; `machines` the
    # per-machine step results from --json, so coverage is exact rather than parsed from stdout.

    def __init__(self, failed, machines):
        self.failed = failed
        self.machines = machines or []

    def __bool__(self):
        return not self.failed

    def failed_configs(self):
        out = {}
        for m in self.machines:
            bad = {s["name"] for s in m.get("steps", []) if s.get("rc") != 0}
            if m.get("error") and not m.get("steps"):
                bad = {"<machine failed before any step>"}
            if bad:
                out[m.get("name") or "?"] = bad
        return out


def _nothing_answered(machines):
    # Why a rc=0 plant applied nothing, or None when the run was real: a machine never reached
    # carries no steps, so an all-unreachable plant is indistinguishable from a clean one by exit
    # code. Narrow: one step result on any host is a real outcome and must not be escalated.
    if not machines:
        return "no per-machine results were reported"
    ran = [m for m in machines if m.get("steps")]
    if ran:
        return None
    names = ", ".join(str(m.get("name") or m.get("ip") or "?") for m in machines[:6])
    return f"all {len(machines)} machine(s) ran zero steps ({names})"


def _machines_from_line(line):
    # Parse a nakon result line, tolerating a glued status line and a "[nakon] " prefix; None when absent.
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
    # Push the bundle to the scoring engine and run `nakon deploy` there; returns the FAILED step
    # lines ([] clean). jobs > 1 passes nakon's --jobs through (per-machine work is atomic, so
    # machines plant in parallel). Staging is per-run: /tmp/nakon-<tag> up, /opt/nakon/<tag> on the
    # engine, created and removed only by the run that owns them.
    ssh_base = [
        "ssh", "-i", str(key), *_engine_opts(host=scoring_ip),
        f"{scoring_user}@{scoring_ip}",
    ]

    # paramiko is baked into the engine template, so this probes instead of installing — a pip
    # install mutated the engine's python mid-deploy and raced concurrent runs.
    probe = subprocess.run(ssh_base + ["sudo python3 -c 'import paramiko'"],
                           capture_output=True, timeout=60)
    if probe.returncode != 0:
        raise RuntimeError(
            f"engine {scoring_ip} cannot import paramiko for the remote nakon deploy: its "
            "template predates the bake — rebuild the engine template (phase 2)")

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
        f"sudo cp -r {tmp_dir}/. {opt_dir}/"
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
        step_line_seen = False
        attempt_started = time.monotonic()
        outcome_unknown = False
        for attempt in range(1, 4):
            proc = subprocess.Popen(ssh_base + [deploy_cmd], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)

            def _pump():
                nonlocal machines_json, step_line_seen
                for line in proc.stdout:
                    print(line, end="")
                    if "FAILED" in line:
                        failed_steps.append(line.strip())
                    if _STEP_LINE_RE.search(line):
                        step_line_seen = True
                    # The structured outcomes are USUALLY the final line, but nakon's status line
                    # can glue the JSON on and some builds prefix "[nakon] ", so decode from "{".
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
                # rc=0 is not "the plant did something": an all-unreachable plant of a strict=False
                # run finishes rc=0 with zero FAILED steps. 2 flaky steps of 41 is tolerated; 41 of
                # 41 is a broken sweep.
                nothing = _nothing_answered(machines_json)
                if nothing:
                    raise RuntimeError(
                        f"nakon deploy reported success but nothing was applied: "
                        f"{nothing}. rc=0 with no per-machine step results means the "
                        f"hosts never answered (or --only selected no machine) — "
                        f"treating that as a completed plant is what let the scale8 "
                        f"soak's pre-domain pass 'succeed' against machines that did not "
                        f"exist. Refusing to continue; check that the boxes are up and "
                        f"reachable, then re-run."
                    )
                return NakonResult(failed_steps, machines_json)
            if failed_steps or attempt == 3:
                break
            # A transport death is only recoverable by re-running the bundle when NOTHING executed:
            # a blind retry re-promoted already-promoted DCs and failed the phase. Retry only a cold death.
            if step_line_seen or time.monotonic() - attempt_started > COLD_DEATH_WINDOW:
                outcome_unknown = True
                print(f"\n  nakon ssh session died (rc={proc.returncode}) after "
                      f"the plant started — outcome unknown, NOT re-running "
                      f"(a blind re-run replants non-idempotent steps). Resume "
                      f"the phase: the resume probes real state and skips "
                      f"completed work.")
                break
            print(f"\n  nakon ssh session died rc=255 before any step ran — "
                  f"retry {attempt}/3 in 20s...")
            failed_steps = []
            machines_json = None
            step_line_seen = False
            attempt_started = time.monotonic()
            time.sleep(20)
        if failed_steps:
            print(f"\n  Nakon plant FAILED steps: {len(failed_steps)}")
            for line in failed_steps[:10]:
                print(f"    {line[:180]}")
        _preserve_failed_run(ssh_base, opt_dir, failed_steps)
        raise RuntimeError(
            f"nakon deploy failed rc={proc.returncode}"
            + (f" ({len(failed_steps)} FAILED steps above)" if failed_steps else "")
            + (f" — the reporting ssh died after the plant started, so the boxes' "
               f"real state is unknown: resume the phase instead of re-running "
               f"(the resume probes what actually planted)" if outcome_unknown else ""))
    except subprocess.TimeoutExpired:
        try:
            # Scoped to THIS run's staging path, so a concurrent run's deploy survives.
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
    # Move a failed plant's staging aside, then print the failed steps' output tails. The
    # per-machine .log is the ONLY record of WHY a step failed and the finally deletes staging.
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
    # Deploy one machine with overridden configs in isolation. The config file doubles as a
    # done-marker (ADDS promotion is skipped when it exists), so write .pending and rename on success.
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
