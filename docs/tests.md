# Tests

The offline suite is a regression net over the deploy-path helpers, not a pipeline test. It never
touches Proxmox, never opens a network connection, and never needs `.env`, credentials, or a
submodule checkout. The real integration gate is
[`verify-competition.py`](usage-agents.md#verify-competitionpy) against a deployed range — nothing
in `tests/` replaces it. `tests/` holds one directory per subsystem; the file set drifts — read it
from `git ls-files tests/`, not from a doc.

## Patching

**Patch the module that owns the name, not one that merely imports it.** `patch.object` only takes
effect where the function *looks the name up*; the owner of any symbol is what
`git grep -n "def <name>"` returns. An unpatched `proxmox_api` in a preflight module makes a real
network call, so this is not cosmetic.

`tests/_deploy_patch.py::dpatch` is the escape hatch: it patches one name in every `deploy_lib`
and `infra` module (plus the `deploy` facade) that binds it, with one shared mock. Use it when the
name crosses a split-module boundary and you do not want to care which module resolves it.

The verify tests load `verify-competition.py` by path for its re-exported gate API; anything
patched is patched in its defining `verifier.*` module — the entrypoint only re-exports.

## Run them

```bash
python3 -m pytest tests/ -q          # the whole suite; prints the current count
python3 -m pytest tests/packet/test_packet_compile.py -q
python3 -m pytest tests/ -q -k frozen
git ls-files tests/                  # the current file list — don't hard-code it here
```

**Use pytest, never `unittest discover`**: discover silently skips pytest-only files, so a real
regression test never runs while the suite reads green.

The red agent is a self-contained folder with its own hermetic pytest suite and its own runner
config (`tlaloc/pyproject.toml`); it is not collected by `tests/`:

```bash
cd tlaloc && python3 -m pytest tests/ -q
python3 -m pytest tlaloc/tests -q      # equivalent, from the repo root
```

`tests/conftest.py` exists for the two process globals `tests/` cannot avoid sharing.
`estate.config` is a process singleton on purpose (multi-node activation re-points the whole
process and stays there), so an activation in one test would silently re-target every later one —
it is reset around each test. And `load_dotenv` writes the real estate identity into `os.environ`
and never removes it, so a test driving a driver's `main()` would hand every later test the LIVE
endpoint — the ambient `TEZ_*` set is stripped at collection and re-stripped after each
test (it is never restored: a test must not inherit a live estate). It also
puts `tests/` on `sys.path` so subdirectory suites can import `_deploy_patch`, and redirects
`Path.home` so a test exercising a locked path can't contend with a real deploy on the same host.

Otherwise each file is standalone `unittest`-style, safe in any order. A handful skip themselves
when an optional tool is missing (e.g. `pyflakes` in `deploy/test_smoke.py`), so a skip is not a
pass.

## Adding one

1. `tests/<subsystem>/test_<subject>.py`, matching style: `unittest.TestCase`, temp dirs,
   `unittest.mock.patch` for anything that would otherwise reach Proxmox or the network.
2. Never import a subprocess — import the module under test directly; dash-named entry points load
   via `importlib.util.spec_from_file_location`, as `tests/deploy/test_redeploy_cli_offline.py`
   does. Set env vars inside the test and restore in `tearDown`.
3. Put the invariant being protected in the module docstring's first line.
4. A new env var your code reads: add its key to `estate/config.py`'s `_SPEC` (the default lives
   there; the canonical `TEZ_` env name is derived from the key) and document it in
   [usage-people.md](usage-people.md#configure-the-event), or list it in `INTERNAL_ENV` in
   `tests/hygiene/test_secret_hygiene.py` with a reason — the drift test fails the suite otherwise,
   by design. `NAKON_*`/`VULNDB_*` names are vendored-tool config read by `vendor/nakon`, not this
   repo.
5. `python3 -m pytest tests/ -q` and confirm the count went up.

## What the suite deliberately does not do

- No Proxmox/Quotient/nakon integration, no live range, no VM.
- No end-to-end phase ordering (closest: `test_smoke.py`'s offline `--plan-only`).
- No timing/performance assertions — budgets are documentation, not tests.

For anything the suite can't reach, the gate is a real deploy plus `verify-competition.py`; see
[e2e-testing.md](e2e-testing.md).
