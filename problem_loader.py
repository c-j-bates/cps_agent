"""
Load problems from a YAML dataset and build ground-truth checkers.

Each problem entry has:
    id:       Unique string identifier.
    problem:  The problem statement.
    solution: Either a literal string (exact match after stripping) or a
              Python function starting with "def check(problem, solution):"
              that returns True/False.

For CSV-based dataset loading and batch evaluation, use Inspect AI
via eval_task.py instead.
"""

from __future__ import annotations

import logging
import yaml
from pathlib import Path

logger = logging.getLogger(__name__)


def load_problems(path: str | Path) -> dict[str, dict]:
    """Return {id: {"problem": ..., "solution": ...}} from a YAML file."""
    with open(path) as f:
        entries = yaml.safe_load(f)

    problems: dict[str, dict] = {}
    for entry in entries:
        pid = entry["id"]
        if pid in problems:
            raise ValueError(f"Duplicate problem id: {pid!r}")
        problems[pid] = {
            "problem": entry["problem"].strip(),
            "solution": entry["solution"],
        }
    return problems


def _is_checker_function(solution: str) -> bool:
    """True if the solution field is a Python checker function."""
    return isinstance(solution, str) and solution.strip().startswith("def check(")


def make_ground_truth(solution_spec: str):
    """Build a GroundTruthChecker from a solution spec.

    Returns a callable(problem, solution) -> (bool, str).
    """
    if _is_checker_function(solution_spec):
        # Compile and extract the check function
        namespace: dict = {}
        exec(compile(solution_spec.strip(), "<checker>", "exec"), namespace)
        checker = namespace["check"]

        def _fn_checker(problem: str, solution: str) -> tuple[bool, str]:
            try:
                result = checker(problem, solution)
            except Exception as e:
                return False, f"Checker raised {type(e).__name__}: {e}"
            if result:
                return True, ""
            return False, f"Checker returned False for: {solution[:80]}"

        return _fn_checker

    # Literal match (strip both sides, case-insensitive)
    expected = str(solution_spec).strip()

    def _literal_checker(problem: str, solution: str) -> tuple[bool, str]:
        if solution.strip().lower() == expected.lower():
            return True, ""
        return False, f"Expected {expected!r}, got: {solution.strip()[:80]!r}"

    return _literal_checker
