"""The eight deploy phases, one function per phase, grouped by what they do.

  cleanup.py    phase 1  tear down the previous range
  engine.py     phases 2-3  engine template + terraform apply #1, per-deploy engine prep
  golden.py     phase 4  golden set + terraform apply #2 + clone bootstrap
  firewall.py   phase 5  in-path firewall bootstrap + engine cutover
  postclone.py  phase 6  repair sweep
  final.py      phase 7  AD domains, final pass, beacons, tz-ready snapshots
  seed.py       phase 8  seed teams / unpause / injects
  finish.py     connect_terraform (between 2 and 3) and finish_deploy (after 8)

Each phase takes the DeployContext prepare() built and touches only that, so a phase boundary
is a call boundary. Every phase prints its own "[N/8] Skipped (resume)" banner when
ctx.from_phase has already passed it; the sequencer (runner.run_pipeline) walks PHASES with
enumerate(..., 1), so a phase's index IS its [N/8] number and its checkpoint value.
"""

from deploy_lib.phases.cleanup import phase1_cleanup
from deploy_lib.phases.engine import phase2_engine_template, phase3_prepare_engine
from deploy_lib.phases.final import phase7_domains_and_final
from deploy_lib.phases.finish import connect_terraform, finish_deploy
from deploy_lib.phases.firewall import phase5_firewall_bootstrap
from deploy_lib.phases.golden import phase4_golden_set
from deploy_lib.phases.postclone import phase6_repair_sweep
from deploy_lib.phases.seed import phase8_seed

__all__ = ["PHASES", "connect_terraform", "finish_deploy"]

# The pipeline, in order.
PHASES = (
    phase1_cleanup,
    phase2_engine_template,
    phase3_prepare_engine,
    phase4_golden_set,
    phase5_firewall_bootstrap,
    phase6_repair_sweep,
    phase7_domains_and_final,
    phase8_seed,
)
