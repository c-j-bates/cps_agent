"""
Debug the NL-rule LLM-judge.

Runs the same scoring template + grader model that `bongard_eval` uses, so
you can see exactly why a candidate was graded the way it was. Useful when
a result JSON hasn't been written yet (run killed early, etc).

Usage:
    python debug_nl_judge.py \
        --reference "the sum of the digits is exactly 12" \
        --candidate "The sum of the four digits equals 12."

Or pass --from-log to pull (reference, candidate) from the most recent
per-problem experiment log (set by --experiment-log-dir, defaults to
experiment_logs/).

Requires ANTHROPIC_API_KEY in environment.
"""

import argparse
import asyncio
import json
import re
from pathlib import Path

from inspect_ai.model import get_model

from eval_task import (
    DEFAULT_NL_JUDGE_MODEL,
    NL_JUDGE_TEMPLATE,
    NL_JUDGE_INSTRUCTIONS,
)


# The same grade-pattern Inspect AI's model_graded_qa uses by default
DEFAULT_GRADE_PATTERN = r"(?i)GRADE\s*:\s*([CPI])(.*)$"


def render_prompt(reference: str, candidate: str) -> str:
    """Fill the NL_JUDGE_TEMPLATE the way Inspect's model_graded_qa would."""
    return NL_JUDGE_TEMPLATE.format(
        criterion=reference.strip(),
        nl_rule=candidate.strip(),
        instructions=NL_JUDGE_INSTRUCTIONS.strip(),
    )


async def run_judge(reference: str, candidate: str, model_name: str) -> dict:
    prompt = render_prompt(reference, candidate)
    model = get_model(model_name)
    result = await model.generate(prompt)
    verdict = result.completion or ""
    match = re.search(DEFAULT_GRADE_PATTERN, verdict, re.MULTILINE)
    if match:
        letter = match.group(1).upper()
        grade = {"C": "CORRECT", "I": "INCORRECT", "P": "PARTIAL"}.get(letter, "UNKNOWN")
    else:
        grade = "NO_MATCH"
    return {
        "model": model_name,
        "prompt": prompt,
        "verdict": verdict,
        "grade": grade,
        "match": match.group(0) if match else None,
    }


def _find_latest_log_pair(log_dir: Path):
    """Pull (target, nl_rule) from the most recent experiment log .json file."""
    jsons = sorted(log_dir.rglob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in jsons:
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        # Our per-problem JSON tree has "execution" which contains branches
        meta = data.get("metadata", {})
        exec_tree = data.get("execution") or []
        # Walk branches looking for extract_rule's response
        nl_rule = None
        for entry in reversed(exec_tree):
            for branch in reversed(entry.get("branches") or []):
                for tn in branch.get("trace") or []:
                    if tn.get("node_id") == "extract_rule":
                        nl_rule = tn.get("response")
                        break
                if nl_rule:
                    break
            if nl_rule:
                break
        # The target rule isn't stored in these logs directly — we need the
        # dataset. User can override via --reference.
        if nl_rule:
            return path, meta, nl_rule
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", help="Ground-truth (target) NL rule")
    ap.add_argument("--candidate", help="Candidate NL rule from the solver")
    ap.add_argument("--model", default=DEFAULT_NL_JUDGE_MODEL,
                    help=f"Grader model (default: {DEFAULT_NL_JUDGE_MODEL})")
    ap.add_argument("--from-log",
                    help="Load candidate from the most recent log under this dir")
    args = ap.parse_args()

    reference = args.reference
    candidate = args.candidate

    if args.from_log and not candidate:
        log_dir = Path(args.from_log)
        path, meta, nl_rule = _find_latest_log_pair(log_dir)
        if nl_rule:
            print(f"(loaded candidate from {path.name})")
            candidate = nl_rule
        else:
            print(f"No candidate found in {log_dir}")
            return

    if not reference or not candidate:
        ap.error("Need both --reference and --candidate (or --from-log + --reference)")

    result = asyncio.run(run_judge(reference, candidate, args.model))
    print("=" * 60)
    print(f"Model:     {result['model']}")
    print(f"Reference: {reference!r}")
    print(f"Candidate: {candidate!r}")
    print("=" * 60)
    print("FULL VERDICT:")
    print(result["verdict"])
    print("=" * 60)
    print(f"MATCH: {result['match']!r}")
    print(f"GRADE: {result['grade']}")


if __name__ == "__main__":
    main()
