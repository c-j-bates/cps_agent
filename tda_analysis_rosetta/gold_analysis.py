"""
Rosetta gold-idea analysis — currently STUBBED OUT.

The Minute Cryptic pipeline computes per-facet coverage of solver
ideas against a hand-coded "gold" idea (the correct solution coded
into the same 4-facet schema). For Rosetta we'd need per-puzzle gold
grammars (lexicon + 14 grammar facets), which require re-running the
generator with a known seed. The BIG-bench task.json that
``datasets/bigbench-rosetta.csv`` was derived from cannot be
reproduced from the public generator (the exact upstream state is
lost), so per-puzzle gold is not currently obtainable for the
existing dataset.

When this gets revisited, plausible paths:
  • regenerate puzzles with the generator (which can emit chosen
    attributes alongside each puzzle), then run those new puzzles
    through the solver experiment, or
  • write a structural decoder that extracts grammar facets directly
    from each puzzle's surface forms, or
  • LLM-extract gold a-la the MC ``code_gold_from_opus``.

Until then ``run_gold_analysis`` is a no-op so the rest of run_tda
continues uninterrupted.
"""

from __future__ import annotations


def run_gold_analysis(strategy_by_problem, gold_output_dir):
    """Stub: skip gold analysis for Rosetta runs."""
    print("  [skipped] Rosetta gold analysis not wired "
          "(see gold_analysis.py docstring).")
    del strategy_by_problem, gold_output_dir  # noqa: F841


def code_gold_from_opus(*args, **kwargs):
    """Stub for API parity with the MC pipeline's gold-coder entry point."""
    raise NotImplementedError(
        "Rosetta gold coding is not implemented. Build the generator-"
        "backed gold pipeline before invoking this."
    )
