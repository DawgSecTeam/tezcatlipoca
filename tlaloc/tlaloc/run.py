"""The loop: one cycle per decision window, deterministic by default.

    kill switch -> reload world -> sensors (scoreboard + blue restores)
      -> artifact health (the re-plant trigger) -> candidates (policy)
      -> digest (compact, budgeted) -> decision (clef, gated)
      -> escalate / pressure budget (the same answers, applied)
      -> hard gate -> execute -> log

Design rules this file exists to keep:

* **the run never depends on a model being up.** Clef down -> deterministic
  candidate #1. LLM down -> clef still runs everything but command authoring.
  Both down -> the deterministic ladder (policy candidate order).
* **the kill switch is checked per command**, not per cycle (it lives in the
  gate, called by the effect template).
* **circuit breakers instead of a silent corpse**: consecutive errors stop the
  loop, an unreachable range stops the loop, and both write a readable event.
* **one action path**: every action — the decision's pick, the escalation, the
  pressure budget — goes through `effects.execute()`, so the gate and the ledger
  cannot be bypassed by a code path that forgot them.
"""

import json
import random
import socket
import time
from pathlib import Path

from . import decide as decide_mod
from . import report as report_mod
from . import seed as seed_mod
from .c2 import FakeMythic, MythicClient
from .ctx import Ctx
from .effects import execute, get as get_effect, reload_registry
from .errors import (EffectParamError, GuardrailError, PolicyError,
                     TlalocError)
from .events import EventLog
from .exec.gate import Gate
from .exec.ssh import SSH
from .intel import load_intel
from .llm import LLM
from .policy import Policy
from .sensors import Sensors
from .state import World

LIVE_HOSTS = ("red01",)

# How many candidate attempts beyond the requested pressure count the loop may
# spend when a target is refused (ssh is our access path) or an effect fails.
# Bounded on purpose: the budget falls through to the next legal target, it does
# not sweep the range.
PRESSURE_FALLTHROUGH = 3


class Loop:
    def __init__(self, ctx, *, policy=None, decider=None, sensors=None, log=None,
                 reports_dir=None, sleeper=None):
        self.ctx = ctx
        self.world = ctx.world
        self.log = log or ctx.log
        self.sensors = sensors if sensors is not None else ctx.sensors
        self.policy = policy or Policy(ctx.cfg, self.world, ctx.intel,
                                      sensors=self.sensors, rng=ctx.rng)
        ctx.policy = self.policy
        self.decider = decider or ctx.decider
        self.reports_dir = reports_dir
        # Injectable so a smoke test does not wait out a 4-minute decision
        # window between cycles (the cadence itself is production behaviour).
        self._sleeper = sleeper or time.sleep
        self.cycle = 0
        self.errors = 0
        self.unreachable = 0
        self.recent = []              # short strings for the digest
        self.records = []
        self.blue_hunting = False

    # -- helpers ------------------------------------------------------------

    def guard_host(self):
        """Refuse to run live off the red VM: a wrong host is a wrong range."""
        run = self.ctx.cfg["run"]
        if self.ctx.dry_run or not run.get("host_guard", True):
            return True
        host = socket.gethostname()
        if any(live in host for live in LIVE_HOSTS):
            return True
        raise TlalocError(
            f"refusing to run the live loop on {host!r} — the red VM is "
            f"{'/'.join(LIVE_HOSTS)} (set run.host_guard: false to override, or use "
            "--dry-run)")

    def verify_artifacts(self):
        """Mark artifacts blue removed (the re-plant trigger).

        A cheap, in-loop check: run each artifact's own `check_cmd` on its box.
        The authoritative down->up proof for scoring belongs to the engine's
        `red-verdict` — this only feeds the loop's own re-plant decision.
        """
        verify_every = int(self.ctx.cfg["run"].get("verify_every_n_cycles", 3))
        if verify_every and self.cycle % verify_every != 0:
            return
        dead = set(self.world.dead_artifacts())
        for artifact in self.world.artifacts():
            check = artifact.get("check_cmd")
            ip = artifact.get("ip")
            fh = self.world.foothold(ip)
            if not check or not fh:
                continue
            key = _artifact_key(artifact)
            # This is the loop's own read-only probe, not an effect — but it is
            # still a command running on a team box, so it goes through the same
            # primitives the effect template uses. The kill switch and the target
            # scope are hard safety (they halt the run); the lint rejection is a
            # skipped artifact, because a bad check_cmd must not take the event
            # down (block_port artifacts deliberately carry no check_cmd at all).
            self.ctx.gate.check_kill_switch()
            self.ctx.gate.ensure_target(ip)
            try:
                self.ctx.gate.ensure_command(check, self.ctx.os_of(ip) or "linux")
            except GuardrailError as exc:
                self.log.emit("artifact_check_rejected", ip=ip,
                              kind=artifact.get("kind"), error=str(exc))
                continue
            try:
                rc, out, _ = self.ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                                              check, timeout=25, windows=fh.get("windows"),
                                              key_path=fh.get("key_path"))
            except TlalocError:
                continue                      # unreachable: not evidence of removal
            alive = rc == 0 and "inactive" not in (out or "")
            if alive:
                dead.discard(key)
                self.world.artifact_ok(_artifact_index(self.world, artifact))
            elif key not in dead:
                dead.add(key)
                self.log.emit("artifact_dead", ip=ip,
                              artifact_kind=artifact.get("kind"), check=check)
        self.world.set_dead_artifacts(dead)

    # -- one cycle ----------------------------------------------------------

    def step(self):
        self.cycle += 1
        self.world.reload()
        self.ctx.gate.check_kill_switch()

        if self.sensors is not None:
            _, counts, restores = self.sensors.observe()
            self.policy.update_counts(counts)
            for event in restores:
                self.recent.append(f"blue_restore {event.get('service')} "
                                   f"{event.get('team')}")
        if self.ctx.c2 is not None:
            try:
                for box, record in self.ctx.c2.callbacks().items():
                    if record.get("seen_at"):
                        self.world.note_callback(
                            box, time.strftime("%Y-%m-%dT%H:%M:%S",
                                               time.gmtime(float(record["seen_at"]))),
                            agent=record.get("agent"),
                            external_id=record.get("external_id"))
            except TlalocError as exc:
                self.log.emit("c2_poll_error", error=str(exc))

        self.verify_artifacts()

        candidates = self.policy.candidates(self.ctx)
        include_blue = (self.ctx.cfg["sensors"].get("blue_hunting_every_n_cycles") and
                        self.cycle % int(self.ctx.cfg["sensors"]
                                         ["blue_hunting_every_n_cycles"]) == 0)
        # The decider composes the digest so its size is derived from what the
        # question bundle costs — see Decider.compose().
        decision = self.decider.next_effect(self.cycle, None, candidates, ctx=self.ctx,
                                           recent=self.recent[-24:],
                                           include_blue_check=bool(include_blue))
        digest = ((decision.bundle or {}).get("state")
                  or self.policy.frame(self.ctx, candidates, recent=self.recent[-24:]))

        if include_blue:
            blued = self.decider.noul("blue_hunting", digest,
                                      "Is blue actively hunting and evicting us right "
                                      "now (restores, logins, firewall changes)?",
                                      cycle=self.cycle)
            self.blue_hunting = bool(blued.answer)
            if self.blue_hunting:
                self.recent.append("blue_hunting yes")

        executed = []
        if decision.chosen is not None:
            executed.append(self._run_candidate(decision, source=decision.source))

        # escalate_now: the model's judgment that it is time for a harder
        # mechanism. Applied by re-deriving the mode of an impact candidate that
        # is already legal — never by inventing an effect.
        escalate = self.decider.noul("escalate_now", digest,
                                     "Escalate to a stronger/unrecoverable mechanism "
                                     "this cycle?", cycle=self.cycle)
        if escalate.answer:
            stronger = self._stronger_impact(decision, candidates, escalate.source)
            if stronger is not None:
                executed.append(self._run_candidate(stronger, source=escalate.source))

        budget = self.decider.pressure_count(digest, cycle=self.cycle)
        wanted = int(budget.answer or 0)
        landed = 0
        # The budget is a target, not an allocation. A candidate the guardrails
        # refuse — ssh is our own access path — must not absorb it: fall through
        # to the next legal target, or a range with a live red team records zero
        # impacts while the budget goes unused.
        for extra in self._extra_pressure(wanted, candidates, decision):
            result = self._run_candidate(extra, source="deterministic")
            executed.append(result)
            if result and result.get("ok"):
                landed += 1
                if landed >= wanted:
                    break

        self.decider.record(self.cycle, executed=[e for e in executed if e])
        # The report's decision-log section reads this list; nothing else fills
        # it, so an empty one silently drops the section from the after-action
        # artifact the organizer reads.
        self.records.append({
            "cycle": self.cycle,
            "kind": "decision_result",
            "source": decision.source,
            "chosen": (decision.chosen or {}).get("id"),
            "top_prob": round(decision.top_prob, 3),
            "executed": [e["id"] for e in executed if e],
        })
        return {"cycle": self.cycle, "decision": decision, "executed": executed,
                "digest": digest, "candidates": len(candidates)}

    def _run_candidate(self, decision, source):
        candidate = decision.chosen
        if candidate is None:
            return None
        try:
            self.policy.validate(candidate, self.ctx)
        except PolicyError as exc:
            self.log.emit("candidate_rejected", cycle=self.cycle,
                          candidate=candidate.get("id"), reason=str(exc))
            return None
        try:
            result = execute(candidate["effect"], candidate["params"], self.ctx,
                             source=source, cycle=self.cycle)
        except EffectParamError as exc:
            # An effect id no module registered (a partial deployment, or a
            # candidate built by hand): a failed candidate with a reason, NOT a
            # cycle error — one missing module must not burn the error budget
            # the breaker watches.
            self.log.emit("candidate_unavailable", cycle=self.cycle,
                          candidate=candidate.get("id"), error=str(exc))
            self.recent.append(f"{candidate['effect']} unavailable")
            return {"id": candidate["id"], "effect": candidate["effect"],
                    "ok": False, "detail": f"unavailable: {exc}"}
        self.recent.append(_short(candidate["effect"], candidate["params"], result))
        return {"id": candidate["id"], "effect": candidate["effect"],
                "ok": result.ok, "detail": result.detail}

    def _stronger_impact(self, decision, candidates, source):
        """The heaviest legal impact candidate, as a new pick (never a new effect)."""
        strongest = self.policy.strongest_impact_mode()
        if not strongest:
            return None
        picked = (decision.chosen or {}).get("id")
        for candidate in candidates:
            if candidate["effect"] != "takedown":
                continue
            if candidate["id"] == picked and candidate["params"].get("mode") == strongest:
                return None
            updated = dict(candidate)
            updated["params"] = {**candidate["params"], "mode": strongest}
            updated["id"] = candidate["id"].replace(
                str(candidate["params"].get("mode")), strongest)
            return type(decision)(decision.question, updated["id"],
                                  source=source, chosen=updated,
                                  note=f"escalate_now -> {strongest}")
        return None

    def _extra_pressure(self, count, candidates, decision):
        """The pressure-budget candidates, in order, best first.

        Returns MORE than `count` when the list allows it: the caller executes
        them until `count` actually land, so a refused target (ssh is our own
        access path) cannot consume the whole budget. The extra attempts are
        bounded — never a sweep of the whole candidate list.
        """
        if count <= 0:
            return []
        taken = (decision.chosen or {}).get("id")
        ceiling = count + PRESSURE_FALLTHROUGH
        out = []
        # Breadth first: within one cycle the pressure budget takes one service per
        # BOX before it takes a second from any box, so a single box cannot absorb
        # the cycle and blue has to split its attention.
        seen_ips = set()
        first, rest = [], []
        for candidate in candidates:
            if candidate["effect"] not in ("takedown", "block_port"):
                first.append(candidate)
                continue
            ip = candidate["params"].get("ip")
            if ip in seen_ips:
                rest.append(candidate)
                continue
            seen_ips.add(ip)
            first.append(candidate)
        for candidate in first + rest:
            if len(out) >= ceiling:
                break
            if candidate["effect"] not in ("takedown", "block_port"):
                continue
            if candidate["id"] == taken:
                continue
            try:
                self.policy.validate(candidate, self.ctx)
                # validate() knows the ROE, not the effect's own refusals (ssh is
                # our access path, a box already failed twice); preflight the real
                # check so a refused candidate cannot absorb the budget.
                get_effect(candidate["effect"]).check(self.ctx, candidate["params"])
            except PolicyError as exc:
                self.log.emit("pressure_candidate_rejected", cycle=self.cycle,
                              candidate=candidate["id"], reason=str(exc))
                self.recent.append(f"pressure skip {candidate['id']}: {exc}")
                continue
            holder = type(decision)("next_effect", candidate["id"],
                                    source="deterministic", chosen=candidate,
                                    note=f"pressure budget ({count})")
            out.append(holder)
        return out

    # -- the run ------------------------------------------------------------

    def run(self, *, max_cycles=None):
        self.guard_host()
        started = time.time()
        self.log.emit("run_start", comp=self.ctx.comp, cycle_max=max_cycles,
                      dry_run=self.ctx.dry_run,
                      mode=self.ctx.cfg["red"].get("assume_breach"))
        self.world.ensure_event_start()

        if (self.ctx.cfg["red"].get("assume_breach", "full") != "off"
                and not self.world.seed_done()):
            seeded = seed_mod.seed_range(self.ctx, dry_run=self.ctx.dry_run)
            self.log.emit("seed", mode=seeded.get("mode"),
                          boxes=len(seeded.get("boxes") or {}),
                          steps=seeded.get("steps"))

        while True:
            if max_cycles is not None and self.cycle >= max_cycles:
                break
            if self.policy.event_over():
                self.log.emit("event_end", cycle=self.cycle)
                break
            try:
                outcome = self.step()
                self.errors = 0
            except GuardrailError:
                self.log.emit("guardrail_halt", cycle=self.cycle)
                raise
            except Exception as exc:                 # a cycle bug must not kill the run
                self.errors += 1
                self.log.error("cycle", exc)
                if self.errors >= int(self.ctx.cfg["run"]["error_halt_after"]):
                    self.log.emit("halt_after_errors", cycle=self.cycle,
                                  errors=self.errors)
                    break
                self._sleeper(self._sleep_seconds())
                continue
            if self._range_unreachable():
                self.unreachable += 1
                if self.unreachable >= int(self.ctx.cfg["limits"]["unreachable_halt_after"]):
                    self.log.emit("halt_range_unreachable", cycle=self.cycle)
                    break
            else:
                self.unreachable = 0
            if max_cycles is not None and self.cycle >= max_cycles:
                break
            if self.policy.event_over():
                break
            self._sleeper(self._sleep_seconds())

        paths = report_mod.write_artifacts(
            self.reports_dir or self.ctx.cfg["run"]["state_dir"], self.world,
            policy=self.policy, sensors=self.sensors, decisions=self.records,
            out_dir=self.reports_dir)
        self.log.emit("run_end", cycle=self.cycle,
                      duration_s=round(time.time() - started, 1), reports=paths)
        return {"cycles": self.cycle, "reports": paths,
                "summary": report_mod.summarize(self.world, self.policy, self.sensors)}

    def _sleep_seconds(self):
        run = self.ctx.cfg["run"]
        if self.policy.teardown_active():
            base = float(run.get("endgame_cycle_min", 1.0)) * 60
        else:
            base = float(run.get("decision_window_min", 4)) * 60
        jitter = float(run.get("jitter_pct", 15)) / 100.0
        rng = self.ctx.rng or random
        return max(base * (1 + rng.uniform(-jitter, jitter)), 5)

    def _range_unreachable(self):
        if self.sensors is None or not self.sensors.enabled:
            return False
        return not self.world.footholds() and not self.sensors.fresh()


def _artifact_key(artifact):
    from .state.world import artifact_key
    return artifact_key(artifact)


def _artifact_index(world, artifact):
    for index, candidate in enumerate(world.artifacts()):
        if candidate is artifact:
            return index
    return 0


def _short(effect, params, result):
    target = params.get("ip") or ""
    what = params.get("service") or params.get("family") or params.get("port") or ""
    return f"{effect} {what}@{target} {'ok' if result.ok else 'fail'}"[:60]


def build_context(cfg, *, comp_dir=None, dry_run=None, clef=None, llm=None, c2=None,
                  sensors=None, ssh=None, rng=None, decider=None):
    """Assemble the live Ctx (the CLI's one construction path; tests use
    tests/helpers.make_ctx with fakes)."""
    reload_registry()
    comp_dir = comp_dir or cfg["run"].get("comp_dir") or "."
    cfg["run"]["comp_dir"] = str(Path(comp_dir).resolve())
    state_dir = Path(cfg["run"]["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)

    intel = load_intel(comp_dir)
    world = World(state_dir, comp=cfg["run"].get("comp") or "")
    log = EventLog(state_dir)
    gate = Gate(cfg, intel.subnets())
    transport = ssh or SSH(cfg, state_dir)

    c2_client = c2
    if c2_client is None and cfg["c2"].get("enabled"):
        c2_client = (MythicClient(cfg, log=log) if not (dry_run if dry_run is not None
                                                        else cfg["run"]["dry_run"])
                     else FakeMythic())
    sensors_client = sensors
    if sensors_client is None:
        sensors_client = Sensors(cfg, intel, world=world, log=log)

    ctx = Ctx(cfg=cfg, world=world, log=log, intel=intel, gate=gate,
              ssh=transport, c2=c2_client, sensors=sensors_client,
              dry_run=bool(cfg["run"]["dry_run"] if dry_run is None else dry_run),
              comp=cfg["run"].get("comp") or "", rng=rng or random.Random())
    policy = Policy(cfg, world, intel, sensors=sensors_client, rng=ctx.rng)
    ctx.policy = policy
    decider = decider or decide_mod.Decider(cfg, world, log, clef=clef,
                                            llm=llm or LLM(cfg, log=log),
                                            policy=policy)
    ctx.decider = decider
    ctx.payload_manifest = None
    return ctx, policy, decider


def run_event(cfg, *, comp_dir=None, max_cycles=None, clef=None, llm=None, c2=None,
              sensors=None, ssh=None, decider=None):
    """Convenience entry point used by the CLI and the smoke test."""
    ctx, policy, _ = build_context(cfg, comp_dir=comp_dir, clef=clef, llm=llm, c2=c2,
                                   sensors=sensors, ssh=ssh, decider=decider)
    loop = Loop(ctx, policy=policy, decider=ctx.decider, sensors=ctx.sensors)
    return loop.run(max_cycles=max_cycles)


def status(cfg, comp_dir=None, *, context=None):
    """Operator status: what the agent believes, in one screen."""
    ctx, policy, _ = context or build_context(cfg, comp_dir=comp_dir, clef=None,
                                             llm=None, c2=None, sensors=None)
    from .effects import catalog
    return {
        "comp": ctx.comp,
        "state_dir": str(ctx.cfg["run"]["state_dir"]),
        "config": {k: ctx.cfg[k] for k in ("red", "clef", "c2", "sensors", "naming")
                   if k in ctx.cfg},
        "world": report_mod.summarize(ctx.world, policy, ctx.sensors),
        "intel": {"source": ctx.intel.source_name, "teams": ctx.intel.teams(),
                  "boxes": len(ctx.intel.boxes())},
        "sensors": (ctx.sensors.status() if ctx.sensors is not None else None),
        "c2": (ctx.c2.operation() if ctx.c2 is not None else None),
        "effects": [e["id"] for e in catalog()],
        "candidates": [c["id"] for c in policy.candidates(ctx)],
    }
