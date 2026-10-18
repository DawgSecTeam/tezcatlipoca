"""tlaloc — the assume-breach CCDC red teamer of the tezcatlipoca range.

Design principles (the migration plan, condensed to what the code enforces):

1. Deterministic by default, model-assisted by exception. Facts come from
   intel/state, bounds from the Compfile (ROE), judgment from clef, generation
   from the LLM arbiter. Nothing else decides.
2. One code path per concern: one exec, one command gate, one effect registry,
   one loop, one state file.
3. Infrastructure belongs to tezcatlipoca; tlaloc owns decisions + effects.
4. Facts belong to nakon (the intel layer); no hand-maintained tables.
5. Assume-breach is the default and is a pipeline stage, not loop logic.
6. Every effect is recoverable-by-design except where the ROE says otherwise;
   restore knowledge is part of the effect and flows to the verdict artifact.
7. Every decision is logged with its question bundle, answer, probabilities,
   threshold, source, and executed effect.
"""

__version__ = "2.0.0-dev"
