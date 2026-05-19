"""
TDA-Based Creativity Measurement for Rosetta-Stone-style linguistic puzzles.

Clone of ``tda_analysis/`` adapted to the linguistics-puzzle domain. The
pipeline structure is identical to the Minute Cryptic version, but the
facet schema is replaced (15 per-puzzle grammar facets — see
``prompts.FACET_KEYS``) and the gold pipeline is currently stubbed
(see ``gold_analysis.py``).

Pipeline:
  1. Code solver outputs into faceted idea trees (LLM Prompts A + B)
  2. Skip span resolution (no inline-text refs in Rosetta facets;
     evidence quotes live only in coder comments)
  3. Batch review of novel facet KEYS (LLM Prompt C) — solvers can
     describe linguistic features the generator does not model
     (e.g. vowel harmony, tone, noun class), so the 15 standard
     facets are a recommendation, not a closed schema. ``novel_``-
     prefixed keys get canonicalized (REMAP / DECOMPOSE / MERGE /
     KEEP) across the dataset.
  4. Compute Jaccard distance matrices over canonical idea frozensets
  5. Run Vietoris-Rips persistence (ripser)
  6. Extract features per (strategy, puzzle) pair
  7. Aggregate and compare across strategies
"""
