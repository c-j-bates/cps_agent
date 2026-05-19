"""
TDA-Based Creativity Measurement for Cryptic Crossword Solvers.

Implements the pipeline from the TDA Creativity Spec v3:
  1. Code solver outputs into faceted idea trees (LLM Prompts A + B)
  2. Resolve substring spans deterministically
  3. Batch review novel mechanism types (LLM Prompt C)
  4. Compute Jaccard distance matrices over canonical idea frozensets
  5. Run Vietoris-Rips persistence (ripser)
  6. Extract 18 features per (strategy, clue) pair
  7. Aggregate and compare across strategies
"""
