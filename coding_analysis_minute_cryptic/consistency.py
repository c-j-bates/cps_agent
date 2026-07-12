from __future__ import annotations

"""
Final consistency checks (Section 11 of spec).

Run after all coding and novel type resolution is complete.
"""

from .ideas import idea_to_frozenset

STANDARD_TYPES = {
    "anagram", "hidden_word", "reversal", "container", "deletion",
    "charade", "double_def", "homophone", "cryptic_def", "&lit",
    "acrostic", "substitution", "letter_selection",
}


def _check_execution_refs(execution: list, clue_text: str, idx: int) -> list[str]:
    """Check execution step references against an evolving clue string.

    Each step can transform the working string: the step's fodder/outer/inner
    are consumed and its result is produced.  Subsequent steps check against
    the transformed string so that intermediate results (e.g. 'LLA' from an
    acrostic) are recognised as valid fodder for the next step.

    A fresh copy of clue_text is used per idea so transformations don't leak
    across ideas or trees.
    """
    warnings = []
    working = clue_text  # per-idea copy (caller must not share across ideas)

    for step in execution:
        if not isinstance(step, dict):
            continue

        # Check references against the current working string
        for key in ("indicator", "fodder", "outer", "inner"):
            ref = step.get(key)
            if isinstance(ref, dict) and ref.get("text") is not None and working:
                if ref["text"] not in working:
                    warnings.append(
                        f"  Entry {idx}: {key} text '{ref['text']}' "
                        f"not found in (possibly transformed) clue"
                    )

        # Apply this step's transformation: replace the fodder (or
        # outer+inner) with the result so subsequent steps can reference it.
        result = step.get("result")
        if result:
            for key in ("fodder", "outer", "inner"):
                ref = step.get(key)
                if isinstance(ref, dict) and ref.get("text") is not None:
                    if ref["text"] in working:
                        working = working.replace(ref["text"], result, 1)
                        break
                elif isinstance(ref, str) and ref in working:
                    working = working.replace(ref, result, 1)
                    break

    return warnings


def check_consistency(coded_result: dict, approved_novel_types: set[str] | None = None
                      ) -> list[str]:
    """Run final consistency checks on a coded result.

    Returns list of warning strings (empty = all good).
    """
    warnings = []
    if approved_novel_types is None:
        approved_novel_types = set()

    trace = coded_result.get("trace", [])
    clue_text = coded_result.get("clue_text", "")

    seen_frozensets = set()

    for idx, entry in enumerate(trace):
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            warnings.append(f"  Entry {idx}: malformed trace entry")
            continue

        facets = entry[2] if isinstance(entry[2], dict) else {}

        # Check: at least one non-null facet
        if all(facets.get(k) is None for k in ("parse", "mechanism", "execution", "output")):
            warnings.append(f"  Entry {idx}: all facets are None")

        # Check: mechanism types are valid
        mechanism = facets.get("mechanism")
        if mechanism is not None:
            mechs = mechanism if isinstance(mechanism, list) else [mechanism]
            for m in mechs:
                if isinstance(m, str) and m not in STANDARD_TYPES and m not in approved_novel_types:
                    if not m.startswith("novel_"):
                        warnings.append(f"  Entry {idx}: unknown mechanism type '{m}'")

        # Check: parse text appears in clue
        parse = facets.get("parse")
        if isinstance(parse, dict) and parse.get("text") is not None and clue_text:
            if parse["text"] not in clue_text:
                warnings.append(
                    f"  Entry {idx}: parse text '{parse['text']}' not found in clue"
                )

        # Check: spans resolve correctly
        if isinstance(parse, dict) and parse.get("text") is not None and parse.get("span") is not None:
            span = parse["span"]
            if clue_text[span[0]:span[1]] != parse["text"]:
                warnings.append(f"  Entry {idx}: parse span mismatch")

        # Check: execution references against evolving clue string
        execution = facets.get("execution")
        if isinstance(execution, list):
            warnings.extend(_check_execution_refs(execution, clue_text, idx))

        # Check: no duplicate frozensets within a solver
        fs = idea_to_frozenset(facets)
        if fs:
            if fs in seen_frozensets:
                warnings.append(f"  Entry {idx}: duplicate frozenset")
            seen_frozensets.add(fs)

    return warnings
