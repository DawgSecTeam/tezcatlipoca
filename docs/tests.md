# Tests

The offline suite is a regression net over the deploy-path helpers, not a pipeline test. It never
touches Proxmox, never opens a network connection, and never needs `.env`, credentials, or a
submodule checkout. The real integration gate is
[`verify-competition.py`](usage-agents.md#verify-competitionpy) against a deployed range — nothing
in `tests/` replaces it.

## Patching

Each name is defined in exactly one module; `patch.object` only takes effect on the module whose
function *looks the name up*, so patch the module that owns the caller, not one that merely
imports it — e.g. `golden_disk_ops` for disk helpers, `nakon_run_ops` for `run_nakon`'s
collaborators, `ubuntu_auth_ops`/`apt_dns_ops`/`service_fixup_ops` for hardening,
`engine_identity_ops`/`engine_cmd_ops` for engine steps, the matching `redeploy_<mode>_ops` for
each redeploy mode, and within `infra/`: `infra.client.proxmox_api`, `infra.guest_exec.*`,
`infra.ownership.*`, `infra.lifecycle.*`, `infra.collisions`/`resources.proxmox_api`. An unpatched
`proxmox_api` in a preflight module makes a real network call.

## Run them

```bash
python3 -m pytest tests/ -q          # the whole suite; prints the current count
python3 -m pytest tests/packet/test_packet_compile.py -q
python3 -m pytest tests/ -q -k frozen
git ls-files tests/                  # the current file list — don't hard-code it here
```

The red agent is a self-contained folder with its own hermetic pytest suite and its own runner
config (`tlaloc/pyproject.toml`); it is not collected by `tests/`:

```bash
cd tlaloc && python3 -m pytest tests/ -q
python3 -m pytest tlaloc/tests -q      # equivalent, from the repo root
```

`conftest.py` isolates the two process globals (`estate.config` and the ambient `TEZ_*` set) and
puts `tests/` on `sys.path` so subdirectory suites can import `_deploy_patch`. Otherwise each file
is standalone `unittest`-style, safe in any order. A handful skip themselves when an optional tool
is missing (e.g. `pyflakes` in `deploy/test_smoke.py`), so a skip is not a pass.

## What the files cover

One directory per subsystem; each holds focused `test_<subject>.py` files. File set and test
count both drift — read them from `git ls-files tests/` and the pytest run above.

| Directory | Guards |
|---|---|
| `deploy/` | Phase order, checkpointing, pause/resume, gate placement, prepare, redeploy modes, the reset ladder, phase-tail and phase-5 offline runs |
| `teardown/` | Ownership/refusal model, tier order, resume, red01 handling, artifacts-before-destruction, team removal, orphan reclaim, operational fixes |
| `estate/` | State document, config precedence, estate create, engine setup, preflight collisions, run ownership, vmid/placement math, multinode |
| `verify/` | Gate correctness (fail-closed coverage, tri-state SKIP, isolation), domains, injects, firewall gate, parallel determinism, freeze guard |
| `scrim/` | Harness stages/resume, scoring reads, endpoints, watchdog, team scaling, LLM endpoints, rehearsal gates |
| `red/` | tlaloc staging/verdict, native red01 identity/network/destroy, liveness/cotenancy, Gaia/Mythic |
| `golden/` | Golden build/freeze/reuse, unbooted DCs, disk sizing, interrupted clones, parallel build, nakon stages, template cloud-init, engine identity |
| `artifacts/` | `artifacts_lib` manifest/plan/collect/report/seal/archive, its CLI, and the scrim harness's calls |
| `portal/` | Portal login/console/routes/ops, console-only PVE tokens, participant remote access |
| `packet/` | Packet compilation/validation, pin semantics, domain/event wiring, the known-broken catalog |
| `infra/` | Proxmox task/timeout/retry, firewall, ssh/guest-exec, interpreter spawns, engine steps, nakon evidence, auth ladders |
| `hygiene/` | Secret/env drift, service-fixup golden, Windows bootstrap/wipe, username lint, comp paths, degradations, scoring account |

`tests/_deploy_patch.py` is not a test: `dpatch(name)` patches one name in every `deploy_lib`
module that binds it.

The verify tests load `verify-competition.py` by path for its re-exported gate API; anything
patched (an SSH hop, `run_concurrent`, `guest_agent_exec_*`, a gate inside `main()`) is patched in
its defining `verifier.*` module — the entrypoint only re-exports.

## Adding one

1. `tests/<subsystem>/test_<subject>.py`, matching style: `unittest.TestCase`, temp dirs, `unittest.mock.patch`
   for anything that would otherwise reach Proxmox or the network.
2. Never import a subprocess — import the module under test directly (dash-named entry points load
   via `importlib.util.spec_from_file_location`, as `test_smoke.py` shows). Set env vars inside the
   test and restore in `tearDown`.
3. Put the invariant being protected in the module docstring's first line.
4. A new env var your code reads: add its key to `estate/config.py`'s `_SPEC` (the default lives
   there; the canonical `TEZ_` env name is derived from the key) and document it in
   [usage-people.md](usage-people.md#configure-the-event), or list it in `INTERNAL_ENV` in
   `tests/hygiene/test_secret_hygiene.py` with a reason — the drift test fails the suite otherwise, by
   design. `NAKON_*`/`VULNDB_*` names are vendored-tool config read by `vendor/nakon`, not this
   repo.
5. `python3 -m pytest tests/ -q` and confirm the count went up.

## What the suite deliberately does not do

- No Proxmox/Quotient/nakon integration, no live range, no VM.
- No end-to-end phase ordering (closest: `test_smoke.py`'s offline `--plan-only`).
- No timing/performance assertions — budgets are documentation, not tests.

For anything the suite can't reach, the gate is a real deploy plus `verify-competition.py`; see
[e2e-testing.md](e2e-testing.md).
