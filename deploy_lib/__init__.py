"""The deploy pipeline behind create-competition.py, split by responsibility.

  cli.py          argument parsing, competition scaffolding, --plan-only
  runner.py       deploy(): lock -> prepare -> walk PHASES -> checkpoint -> finish
  prepare.py      prepare(): the ordered pre-phase steps, producing a DeployContext
  gates.py        EVERY pre-flight / ownership / state-version refusal, in one place
  context.py      DeployContext (the object the phases share) + checkpoint
  stages.py       the per-step records prepare() fills in
  inputs.py       read Compfile/boxes/users/state/injects from disk
  secrets.py      carry or mint the competition's credentials
  placement.py    multi-node placement + the engine lock
  configs.py      generated nakon/stage configs + golden hashes + frozen gate
  tfinputs.py     terraform.tfvars.json + engine mgmt IP env
  targets.py      enumerate the VMs the deploy touches
  golden_plan.py  golden hash entries + the destroy/keep decisions they drive
  coverage.py     plant-coverage bookkeeping
  failure.py      failure streak + tolerated-failure ledger
  phases/         the eight phase functions (see phases/__init__.py)
"""
