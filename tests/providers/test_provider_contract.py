"""The provider contract: every platform's apply_team_boxes takes (ctx, sources).

`deploy_lib/phases/golden.py` owns the golden-set bookkeeping and hands the platform layer a single
`sources` map (vm_name -> source template vmid). `_estate.create_team_boxes` is the only caller:

    providers.of_ctx(ctx).apply_team_boxes(ctx, sources)

The proxmox provider originally carried a copy of the PHASE's apply_team_boxes (three arguments:
ctx, golden_ids, golden_ids_by_slot) rather than the body it was supposed to implement, so every
proxmox deploy died at phase 4 with

    TypeError: apply_team_boxes() missing 1 required positional argument: 'golden_ids_by_slot'

(run-965f1970). These tests pin the signature for both platforms so the split cannot silently
drift apart again.
"""

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from providers.gcp import pipeline as gcp_pipeline  # noqa: E402
from providers.proxmox import pipeline as proxmox_pipeline  # noqa: E402


class ProviderContractTest(unittest.TestCase):
    def test_every_provider_takes_ctx_and_sources(self):
        for platform, module in (("gcp", gcp_pipeline), ("proxmox", proxmox_pipeline)):
            params = list(inspect.signature(module.apply_team_boxes).parameters)
            self.assertEqual(
                params, ["ctx", "sources"],
                f"{platform}.apply_team_boxes must take (ctx, sources) — the phase layer builds "
                f"the sources map and the provider only mutates its own estate")

    def test_the_phase_calls_the_provider_with_exactly_two_arguments(self):
        # _estate.create_team_boxes is the seam between the phase layer and the platform layer.
        from deploy_lib.phases import _estate
        src = inspect.getsource(_estate.create_team_boxes)
        self.assertIn("apply_team_boxes(ctx, sources)", src,
                      "the phase layer must call the provider with (ctx, sources)")
