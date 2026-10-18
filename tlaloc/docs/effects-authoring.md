# Authoring an effect

An effect is one module in `tlaloc/effects/`, one class, one registry entry. Read
`tlaloc/effects/base.py` (the contract) and `tlaloc/effects/__init__.py` (the
`execute()` template that wraps every call) before writing anything — they are
short and they are the law.

## The shape

```python
from .base import Effect, Result, register, root_cmd, can_root
from ..errors import PolicyError

@register
class MyEffect(Effect):
    id = "my_effect"                # snake_case, matches the module name
    name = "one line"
    description = "what it does + when to prefer it"   # fed to the arbiters
    params = {
        "ip":      {"type": "str", "required": True, "desc": "target"},
        "service": {"type": "str", "required": True, "desc": "unit/slug"},
        "command": {"type": "str", "freeform": True, "max": 200,
                    "desc": "shell text — linted by the gate"},
    }
    tags = frozenset({"pressure", "impacty"})
    needs = {"foothold": True, "root": False, "c2": False, "os": "any"}

    def check(self, ctx, params):
        super().check(ctx, params)      # declarative preconditions
        # raise PolicyError for a bounded-by-ROE no; that is a rejected
        # candidate, never a crash.

    def run(self, ctx, params):
        ...                             # do the thing
        return Result(True, "what happened", data={...})
```

## Hard rules

1. **`run()` never touches the gate.** The template runs the kill switch,
   `ensure_target`, free-form lint and `check()` before `run()`. An effect that
   re-implements a gate is the defect this design exists to remove. (You may
   call `ctx.gate.ensure_retries` — it is a per-target anti-grind counter, not a
   safety gate.)
2. **`ok=True` means the objective was met.** Every give-up path returns
   `ok=False` with a detail that says so. A cap hit, a `did not hold`, a timeout
   that may already have landed — all `ok=False`. This is load-bearing: the seed
   records access/plants from that flag, and blue-facing claims are graded on it.
3. **Attribution is code-side.** An effect that took a scored service down sets
   `Result.impact = {"ip": ..., "service": ..., "mode": ..., "detail": ...}`. The
   template writes the ledger row; no caller has to remember a `service=` param.
4. **Every recoverable mutation carries its undo.** Set
   `Result.restore = {"recipe": <str|list[str]>, "secrets": <str|None>}` for
   anything blue or the organizer must be able to reverse, and use a MOVE or a
   COPY (never `rm`, never truncate-in-place) so the original is always on the
   box. `secrets` (an original password, a key) is only ever rendered into the
   0600 organizer artifact.
5. **Record plants from what the install did**, not from a C2 check-in that
   gates the record — a slow check-in must not turn a live plant into a silent
   absence. Return `Result.artifacts = [{"ip","kind","detail","check_cmd",
   "paths"}]` and say the outcome in `detail`.
6. **Prefer the foothold's own credentials through `ctx.ssh`** with
   `root_cmd(cmd, fh)` when root is required. `root_cmd`'s argument order is
   `(command, foothold)`.
7. **Dry-run safe.** `run()` is not called at all under `ctx.dry_run` (the
   template short-circuits), so a fresh effect is exercised by
   `tests/test_effects.py::test_every_registered_effect_survives_dry_run` for
   free — just keep `params` honest.

## Tags

`access | c2 | pressure | persistence | endgame` — the capability class, AND
`passive | impacty | unrecoverable` — the danger class. `impacty` marks an effect
that can take a scored service down (policy caps it). `unrecoverable` marks the
effects whose undo needs the organizer (rotated credential, locked account, port
block): the ladder's top tier is what unlocks them.

## Where the pieces come from

- transport: `ctx.ssh.ssh(ip, user, password, cmd, timeout=, windows=, key_path=)`,
  `ctx.ssh.ssh_ps(ip, user, password, script, ...)`, `ctx.ssh.run_local(cmd)`,
  `ctx.ssh.sftp_put/get(...)`. Windows wrapping lives in `exec/winexec.py` only.
- facts: `ctx.intel` (`boxes()`, `box_by_ip()`, `team_of()`, `candidate_pairs(ip)`,
  `known_creds()`, `hints_for(ip)`, `services_for(ip)`, `port_checks_for(ip)`,
  `unit_candidates(slug)`).
- state: `ctx.world` (`foothold(ip)`, `note(ip, text)`, `add_cred`, `add_foothold`,
  `elevate_foothold`, `target(ip)`, `creds_tried`, `count_cred_try`, ...). The
  template owns `add_impact` / `add_artifact` / `add_restore` via `Result`.
- C2: `ctx.c2` (`Mythic`), `None` when `c2.enabled` is false — `needs={"c2": True}`
  makes that a clean rejection instead of a crash.
- bounds: `ctx.policy` (`allowed_impact_modes()`, `can_impact(team)`,
  `unrecoverable_unlocked()`, `ladder()`). Raise `PolicyError` from `check()`.

## Tests

`tests/helpers.py` has `make_ctx()` (a `FakeSSH`, `FakeC2`, temp `World`,
`BlindIntel`/`make_intel(comp_dir)`), one scripted transport, no `object.__new__`.
Add a test module per effect that scripts the transport responses and asserts:
the success path, the give-up path (`ok=False`), that the ROE rejection is a
`PolicyError` (not a crash), and that the restore recipe round-trips.

Run the suite with the repo's interpreter:

```
python3 -m pytest tests/ -q
```

`unittest discover` is NOT a runner here (it silently skips pytest-only files —
the earlier agent's defect this repo's CI is built to prevent).
