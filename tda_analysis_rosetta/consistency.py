from __future__ import annotations

"""
Final consistency checks for Rosetta-coded traces.

The Minute Cryptic equivalent checks text-substring references against
the clue (parse.text in clue, execution fodder/outer/inner against the
evolving clue string) — these don't apply to Rosetta, where the facet
schema is structural and evidence quotes live in coder comments only.

What we DO check:
  • Every trace entry has at least one non-null facet.
  • Every facet key is either standard (in ``FACET_KEYS``), an
    approved-novel name (PROMPT_C KEEP'd or REMAP'd target), or a
    ``novel_``-prefixed pending-review key. Anything else is a coder
    bug.
  • ``marker_type`` (when present in a morphology facet) is one of
    SUFFIX | PREFIX | PARTICLE_AFTER.
  • No two entries within a single solver collapse to the same
    canonical frozenset (the coder failed to dedup).

The facet schema is OPEN, parallel to MC's mechanism vocabulary:
real solvers may identify (or invent) linguistic features the
generator does not model — vowel harmony, tone, noun class, etc. —
and ``check_consistency`` must accept them.

The function signature matches the MC version so run_tda.py can call
it identically.
"""

from .ideas import idea_to_frozenset
from .prompts import FACET_KEYS

_VALID_MARKER_TYPES = {"SUFFIX", "PREFIX", "PARTICLE_AFTER"}
_MORPHOLOGY_FACETS = {
    "noun_case", "noun_plurality", "det_case", "det_plurality",
    "adj_case", "adj_plurality", "verb_tense", "verb_plurality",
}


def check_consistency(coded_result: dict,
                      approved_novel_types: set[str] | None = None
                      ) -> list[str]:
    """Run consistency checks on a coded Rosetta result.

    Returns list of warning strings (empty = all good).
    ``approved_novel_types`` is the set of facet-key names PROMPT_C has
    KEEP'd (or REMAP'd onto) — these count as valid even without the
    ``novel_`` prefix, mirroring how MC treats post-Prompt-C-approved
    mechanism types.
    """
    warnings: list[str] = []
    approved = approved_novel_types or set()

    trace = coded_result.get("trace", [])
    schema = set(FACET_KEYS)
    seen_frozensets = set()

    for idx, entry in enumerate(trace):
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            warnings.append(f"  Entry {idx}: malformed trace entry")
            continue

        facets = entry[2] if isinstance(entry[2], dict) else {}

        # Check 1: at least one non-null facet
        if all(facets.get(k) is None for k in facets):
            warnings.append(f"  Entry {idx}: all facets are None")

        # Check 2: facet keys are valid. Valid = standard, approved-
        # novel (post-Prompt-C), or ``novel_*`` (pending review).
        for k in facets.keys():
            if k in schema:
                continue
            if k in approved:
                continue
            if isinstance(k, str) and k.startswith("novel_"):
                continue
            warnings.append(
                f"  Entry {idx}: unknown facet key '{k}' "
                f"(not standard, not in approved novel types, "
                f"and not prefixed novel_)"
            )

        # Check 3: marker_type values are valid in morphology facets
        for fac in _MORPHOLOGY_FACETS:
            val = facets.get(fac)
            if isinstance(val, dict):
                mt = val.get("marker_type")
                if mt is not None and mt not in _VALID_MARKER_TYPES:
                    warnings.append(
                        f"  Entry {idx}: {fac}.marker_type='{mt}' "
                        f"not in {sorted(_VALID_MARKER_TYPES)}"
                    )

        # Check 4: no duplicate frozensets within a solver
        fs = idea_to_frozenset(facets)
        if fs:
            if fs in seen_frozensets:
                warnings.append(f"  Entry {idx}: duplicate frozenset")
            seen_frozensets.add(fs)

    return warnings
