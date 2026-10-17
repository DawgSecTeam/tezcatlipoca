"""improvise — the model authors ONE command for a bespoke goal, and an explicit
verification step decides whether it worked.

v1's improvise tactic was the "AI for implementation" lever: the catalog covers
the standard path, so when a box needs something bespoke (a weird service, a
SUID escape the privesc module only noted) the director handed the model a
one-line goal. The mechanics are worth keeping — the goal, the target's OS and
the box's own facts go to the model, the text it returns is screened before it
runs, and the foothold's own credentials carry it. Three of v1's behaviours are
exactly why v2 exists, and this module is built the other way round:

* **attribution was a substring sniff.** v1's policy judged a goal "impacty"
  with ``any(w in goal for w in _IMPACTY_WORDS)`` and the ledger inherited the
  same guess. Here ``check()`` takes the deterministic word-boundary floor
  (``decide.keyword_intent``) to decide whether the ROE's impact gate applies,
  and ``Result.impact`` is set from the decision layer's ``command_intent``
  answer (``decide.classify_command``, clef with that floor underneath it) — one
  classification, in one place, that the replay harness can re-run.
* **success was ``ok = any(rc == 0)``** over a volley of up to twelve blind
  commands. Here the author must also return a *verification* command; that
  command is linted and run like any other, and ``decide.step_verified`` judges
  the pair with the verification's own result as its deterministic default.
  ``ok=True`` requires the command to have run (rc 0) **and** the verification
  to have agreed. A round nothing verified is ``ok=False`` — never a success by
  exit status, and never a success by inference.
* **round N+1 could be built on round N that never worked.** Here one command
  (plus its verification) runs per call, and nothing is stacked on an
  unverified step: an unverified round ends the call. Grinding is bounded
  twice over — the loop owns ``red.budget.improvise_rounds``, and ``check()``
  refuses a third attempt on a box that already failed twice.

Linting. ``command`` is declared a free-form param, so when the *caller* passes
one the shared ``execute()`` template screens it with every other param. Text
that only exists after the model answers cannot be screened there — it is
screened inside ``run()`` with the same primitive the template uses,
``ctx.gate.ensure_command(text, target_os)``, on the TARGET's OS pattern set
(v1's lint was POSIX-only in front of cmd.exe). A model-authored command that
trips it fails the round, logged; a bad suggestion is not a broken design, so it
does not halt the event. Nothing runs unlinted.

Nothing is invented: without the authoring client (``llm.enabled``, or a caller
that already has the text) there is no command, and the result is ``ok=False``
with a detail the loop can act on — it falls back to the deterministic ladder.
"""

from ..decide import keyword_intent
from ..errors import EffectParamError, GuardrailError, LLMError, PolicyError
from .base import Effect, Result, can_root, register, root_cmd

# The per-command bound comes from the shared limits block (no private knob:
# an improvised round is an SSH action like any other).
DEFAULT_TIMEOUT_KEY = "action_timeout"

MODE = "improvise"


def goal_of(params):
    """The one-line objective of record.

    ``policy`` emits ``goal``; a caller that already has the text emits
    ``command`` and the command is then its own goal. With neither there is
    nothing to run and nothing to verify against — that is a shape error, not a
    round to attempt blind.
    """
    goal = str(params.get("goal") or "").strip()
    if goal:
        return goal
    command = str(params.get("command") or "").strip()
    if command:
        return command
    raise EffectParamError("improvise needs a goal (or a command) to work on")


def llm_of(ctx):
    """The authoring client.

    The runtime that wires the model is ``build_context``: it builds
    ``decide.Decider(llm=LLM(...))`` and hangs the decider on the context, so
    that is where the client lives. ``ctx.llm`` is also honoured because it is
    the more obvious seam (and the one a future loop may expose) — either way
    there is exactly one authoring path, ``llm.author_command``; no effect makes
    its own HTTP call.
    """
    llm = getattr(ctx, "llm", None)
    if llm is None:
        llm = getattr(getattr(ctx, "decider", None), "llm", None)
    return llm


def intent_of(ctx, command, os_name):
    """(intent, source) — the decision layer's classification, else the floor."""
    decider = getattr(ctx, "decider", None)
    if decider is not None and hasattr(decider, "classify_command"):
        try:
            decision = decider.classify_command(command, os_name)
            ctx.log.emit("command_intent", command=command[:120],
                         intent=decision.answer, source=decision.source,
                         top_prob=round(float(decision.top_prob or 0.0), 3))
            return decision.answer, decision.source
        except Exception as exc:            # a classifier fault is not a failed round
            ctx.log.emit("command_intent_error", error=str(exc))
    return keyword_intent(command), "deterministic"


def facts_for(ctx, ip, fh, goal):
    """What the author is allowed to see: the box, not the range.

    Same shape as v1's improvise context (host/user/root/ports/notes) plus the
    scored service slugs, because they are what a bespoke goal is usually about.
    """
    target = ctx.world.target_record(ip) or {}
    ports = [p.get("port") for p in (target.get("ports") or []) if p.get("port")]
    return {
        "host": ip,
        "os": "windows" if fh.get("windows") else "linux",
        "user": fh.get("user"),
        "root": bool(can_root(fh)),
        "goal": goal,
        "services": [str(s) for s in (ctx.intel.services_for(ip) or [])][:8],
        "open_ports": ports[:15],
        "recent_notes": [str(n) for n in (target.get("notes") or [])][-5:],
    }


def escalate(cmd, fh, as_root):
    """``as_root`` rides the foothold's label — and only a label we know works.

    v1 reshaped the text itself when the goal wanted root. Here the shared
    ``root_cmd`` owns the wrapping, and free-form text never rides an
    ``escape:``/``capexec:`` wrapper: those break on arbitrary quoting (their
    own warning in effects/base.py), so the round runs unprivileged instead and
    reports what it actually achieved. A label-less non-root foothold is left
    alone too — inventing a sudo path the box never gave us is how a step
    reports success it never had.
    """
    if not as_root:
        return cmd
    esc = (fh.get("escalation") or "")
    if esc in ("sudo", "sudo_pw") or fh.get("kind") == "root":
        return root_cmd(cmd, fh)
    return cmd


def run_cmd(ctx, ip, fh, cmd, as_root=False):
    """One command through the transport, on the foothold's own credentials.

    Windows goes through the single owner of that idiom (``exec/winexec.py``,
    called by ``exec/ssh.py``); a double-quote-free one-liner rides the legacy
    wrap, anything quote-heavier goes as an EncodedCommand.
    """
    user = fh.get("user")
    password = fh.get("password") or ""
    key_path = fh.get("key_path")
    timeout = int(ctx.cfg["limits"].get(DEFAULT_TIMEOUT_KEY, 90))
    to_run = escalate(cmd, fh, as_root)
    windows = bool(fh.get("windows"))
    if windows and '"' in to_run:
        return ctx.ssh.ssh_ps(ip, user, password, to_run, timeout=timeout,
                              key_path=key_path)
    return ctx.ssh.ssh(ip, user, password, to_run, timeout=timeout,
                       windows=windows, key_path=key_path)


def digest_for(ctx, goal):
    """The tiny state ``step_verified`` is asked about (it reads ``t``)."""
    policy = getattr(ctx, "policy", None)
    clock = None
    if policy is not None and hasattr(policy, "elapsed_min"):
        try:
            clock = (f"{int(policy.elapsed_min())}/"
                     f"{int(policy.red['duration_min'])}")
        except Exception:
            clock = None
    return {"t": clock, "goal": goal[:120]}


def service_for(ctx, ip, goal, command):
    """The scored service this round is about, when the text names one.

    Derived from intel rather than guessed: an impact row with a service name
    that is not on the box is worse than no attribution at all.
    """
    text = f"{goal} {command}".lower()
    for name in (ctx.intel.services_for(ip) or []):
        slug = str(name).lower().strip()
        if slug and slug in text:
            return str(name)
    return None


@register
class Improvise(Effect):
    id = "improvise"
    name = "author one command for a goal (LLM)"
    description = ("The model writes ONE shell/PowerShell command for a bespoke "
                   "goal (a service the catalog does not cover, a one-off path) "
                   "plus a command that verifies it, both screened before they "
                   "run. Use it when every bounded effect is wrong for this box "
                   "and the goal is narrow; pass service= when the goal takes a "
                   "scored service down so the impact ledger is right. Needs a "
                   "foothold, and the authoring model (llm.enabled) unless the "
                   "caller supplies command= itself.")
    params = {
        "ip": {"type": "str", "required": True, "desc": "target box"},
        "goal": {"type": "str", "max": 200,
                 "desc": "one-line objective (policy emits this)"},
        "command": {"type": "str", "freeform": True, "max": 200,
                    "desc": "use this text instead of authoring one "
                            "(screened by the template)"},
        "service": {"type": "str",
                    "desc": "scored service the goal targets, for the ledger"},
        "as_root": {"type": "str",
                    "desc": "'true' to ride the foothold's sudo label"},
    }
    tags = frozenset({"access", "impacty"})
    needs = {"foothold": True, "root": False, "c2": False, "os": "any"}

    def check(self, ctx, params):
        super().check(ctx, params)
        ip = params["ip"]
        goal = goal_of(params)
        # The deterministic floor decides whether the ROE's impact gate applies
        # at all (v1 gated improvise the same way, with a substring sniff; this
        # is the word-boundary read, and it never consults the model because
        # check() runs under --dry-run too).
        policy = getattr(ctx, "policy", None)
        if policy is not None and keyword_intent(goal) == "takedown":
            ok, why = policy.can_impact(ctx.intel.team_of(ip))
            if not ok:
                raise PolicyError(f"the improvise goal reads as an impact move "
                                  f"(deterministic floor): {why}")
        if ctx.world.failure_count(self.id, ip) >= 2:
            raise PolicyError(f"improvise already failed twice on {ip} — switch "
                              "target or goal")

    def run(self, ctx, params):
        ip = params["ip"]
        fh = ctx.world.foothold(ip)
        if fh is None:
            return Result(False, f"no foothold on {ip}")
        os_name = ctx.os_of(ip)
        goal = goal_of(params)
        as_root = str(params.get("as_root") or "").lower() == "true"

        proposal, command = self._obtain(ctx, params, ip, fh, goal, os_name)
        if isinstance(proposal, Result):
            return proposal                      # ok=False, already explained
        if not command:
            return Result(False, "the author returned no command",
                          data={"goal": goal})

        # The caller's command rode the template's lint; the author's text only
        # exists now, so it gets the same primitive, on the target's OS.
        rejected = self._lint(ctx, command, os_name)
        if rejected:
            ctx.log.emit("improvise_rejected", ip=ip, os=os_name,
                         command=command[:120], reason=rejected)
            return Result(False, f"the command was rejected by the lint: "
                                 f"{rejected}",
                          data={"goal": goal, "command": command})
        try:
            rc, out, err = run_cmd(ctx, ip, fh, command, as_root=as_root)
        except Exception as exc:             # TransportError: the box is gone
            return Result(False, f"the command did not run on {ip}: "
                                 f"{type(exc).__name__}: {exc}",
                          data={"goal": goal, "command": command})

        # The verification step exists to prove a change landed; a command that
        # already failed has nothing to prove and does not get one.
        probe = (self._probe(ctx, ip, fh, proposal, os_name, as_root)
                 if rc == 0 else None)
        intent, intent_source = intent_of(ctx, command, os_name)
        trace = {"goal": goal, "command": command,
                 "verify": (probe or {}).get("command"),
                 "rc": rc, "output": (out or "")[:400], "intent": intent,
                 "intent_source": intent_source,
                 "why": (proposal or {}).get("why"), "probe": probe}

        if rc != 0:
            return Result(False, f"'{goal}' on {ip}: the command exited rc={rc} "
                                 f"({(err or out).strip()[:200] or 'no output'})",
                          data=trace)

        verified, how = self._verified(ctx, goal, command, rc, out, probe)
        trace["verified"] = verified
        trace["verify_how"] = how
        if verified is None:
            return Result(False, f"'{goal}' on {ip}: the command ran (rc=0) but "
                                 "nothing verified it — claiming no success",
                          data=trace)
        if not verified:
            return Result(False, f"'{goal}' on {ip}: not verified ({how})",
                          data=trace)

        return self._succeeded(ctx, params, ip, goal, command, intent,
                               intent_source, how, trace)

    # -- the pieces ---------------------------------------------------------

    def _obtain(self, ctx, params, ip, fh, goal, os_name):
        """(proposal, command) or (Result(False), '') when there is nothing to run."""
        supplied = str(params.get("command") or "").strip()
        if supplied:
            return {"command": supplied, "verify": None,
                    "why": "supplied by the caller"}, supplied
        llm = llm_of(ctx)
        if llm is None or not getattr(llm, "enabled", False):
            return Result(False, "no command author available (llm.enabled / "
                                 "llm.base_url) — the loop falls back to the "
                                 "deterministic ladder",
                          data={"goal": goal}), ""
        try:
            proposal = llm.author_command(goal, facts_for(ctx, ip, fh, goal),
                                          os_name)
        except LLMError as exc:
            return Result(False, f"authoring failed: {type(exc).__name__}: {exc}",
                          data={"goal": goal}), ""
        except Exception as exc:             # a broken client is still not a crash
            ctx.log.emit("improvise_author_error", ip=ip,
                         error=f"{type(exc).__name__}: {exc}")
            return Result(False, f"authoring failed: {type(exc).__name__}: {exc}",
                          data={"goal": goal}), ""
        if not isinstance(proposal, dict):
            return Result(False, "authoring failed: the author returned no JSON "
                                 "object", data={"goal": goal}), ""
        return proposal, str(proposal.get("command") or "").strip()

    @staticmethod
    def _lint(ctx, command, os_name):
        """The rejection reason, or None. Same primitive the template uses."""
        try:
            ctx.gate.ensure_command(command, os_name)
            return None
        except GuardrailError as exc:
            return str(exc)

    def _probe(self, ctx, ip, fh, proposal, os_name, as_root):
        """The author's own verification command, run like any other command.

        Its exit status is the in-band proof (a check command reports through
        its status, so `test -s …`/`grep -q …` are legitimate verifications).
        None means the author proposed no verification at all — which the caller
        reads as "nothing verified this step", never as success.
        """
        verify = str((proposal or {}).get("verify") or "").strip()
        if not verify:
            return None
        rejected = self._lint(ctx, verify, os_name)
        if rejected:
            return {"command": verify, "rc": None, "out": "", "ok": False,
                    "reason": f"verification rejected by the lint: {rejected}"}
        try:
            vrc, vout, verr = run_cmd(ctx, ip, fh, verify, as_root=as_root)
        except Exception as exc:
            return {"command": verify, "rc": None, "out": "", "ok": False,
                    "reason": f"verification did not run: "
                              f"{type(exc).__name__}: {exc}"}
        return {"command": verify, "rc": vrc,
                "out": (vout or verr or "")[:400], "ok": vrc == 0}

    @staticmethod
    def _verified(ctx, goal, command, rc, out, probe):
        """(True|False|None, how) — the explicit verification step.

        ``None`` means nothing could verify the step and the caller must not
        read that as success (v1 read ``rc == 0`` there). The decision layer is
        asked when it is wired, with the author's verification result as its
        deterministic default — so a clef outage degrades to the in-band check,
        never to the exit status.
        """
        probe_ok = probe.get("ok") if probe else None
        decider = getattr(ctx, "decider", None)
        judge = None
        if decider is not None and hasattr(decider, "step_verified"):
            evidence = (out or "").strip()[:200]
            if probe:
                evidence += (f" | verify[{probe.get('rc')}]: "
                             f"{probe.get('out') or probe.get('reason') or ''}")[:240]
            try:
                judge = decider.step_verified(digest_for(ctx, goal), command, rc,
                                             evidence, default=bool(probe_ok))
            except Exception as exc:         # a judge fault must not claim success
                ctx.log.emit("step_verified_error", error=str(exc))
        if judge is not None:
            how = (judge.note if getattr(judge, "note", None)
                   else f"step_verified via {judge.source}")
            if judge.answer and probe_ok is False:
                return False, "the author's verification command did not confirm it"
            return bool(judge.answer), how
        if probe_ok is None:
            return None, "no verification command and no decision layer"
        if probe_ok:
            return True, "the author's verification command agreed"
        return False, (probe.get("reason")
                       or "the author's verification command did not confirm it")

    def _succeeded(self, ctx, params, ip, goal, command, intent, intent_source,
                   how, trace):
        detail = (f"'{goal}' on {ip}: ran and verified ({how})")
        ctx.world.note(ip, f"improvise[{intent or '?'}]: {goal} -> "
                           f"{command[:120]}")

        impact = None
        if intent == "takedown":
            service = params.get("service") or service_for(ctx, ip, goal, command)
            if service:
                impact = {"ip": ip, "service": service, "mode": MODE,
                          "detail": f"authored command: {command[:120]}"}
            else:
                ctx.log.emit("improvise_impact_unattributed", ip=ip,
                             command=command[:120],
                             why="takedown intent but no scored service was named")
        # The command changed a box and its effect is not derivable from here, so
        # the undo names the exact text that did it instead of pretending to a
        # recipe. (No artifact row: artifacts are plants, and the loop re-plants
        # what it thinks blue removed — an improvised one-off is not that.)
        restore = {"recipe": [f"# no automatic undo — reverse by hand: {command}"],
                   "secrets": None}
        # A takedown with a known service gets the one step that actually undoes
        # it, appended to the honest note (the comment stays first so a recipe is
        # always readable as "what ran, then what reverses it").
        if impact and params.get("service"):
            unit = str(params["service"])
            unit = unit if unit.endswith(".service") else f"{unit}.service"
            restore["recipe"].append(
                f"Start-Service '{params['service']}'" if ctx.is_windows(ip)
                else f"systemctl start {unit}")
        return Result(True, detail, data=trace, impact=impact, restore=restore)
