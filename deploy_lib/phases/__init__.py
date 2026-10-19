"""The eight deploy phases, one function per phase, grouped by what they do."""

from deploy_lib.phases.cleanup import phase1_cleanup
from deploy_lib.phases.engine import phase2_engine_template, phase3_prepare_engine
from deploy_lib.phases.final import phase7_domains_and_final
from deploy_lib.phases.finish import connect_estate, finish_deploy
from deploy_lib.phases.firewall import phase5_firewall_bootstrap
from deploy_lib.phases.golden import phase4_golden_set
from deploy_lib.phases.postclone import phase6_predomain_pass
from deploy_lib.phases.seed import phase8_seed

__all__ = ["PHASES", "connect_estate", "finish_deploy"]

# runner.run_pipeline walks this with enumerate(..., 1): a phase's index here IS its
# [N/8] banner number and its checkpoint value.
PHASES = (
    phase1_cleanup,
    phase2_engine_template,
    phase3_prepare_engine,
    phase4_golden_set,
    phase5_firewall_bootstrap,
    phase6_predomain_pass,
    phase7_domains_and_final,
    phase8_seed,
)
