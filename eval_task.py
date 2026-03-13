"""
Inspect AI evaluation task for problem solving.

Defines the dataset loader, solver (wrapping agents), and scorer
with flexible per-row checker dispatch.

Usage via CLI:
    inspect eval eval_task.py -T dataset_path=datasets/test_puzzles.csv

Usage via Python:
    from inspect_ai import eval as inspect_eval
    from eval_task import problem_eval

    logs = inspect_eval(problem_eval(dataset_path="datasets/test_puzzles.csv"))
"""

from __future__ import annotations

import logging
import re
from typing import Any

from inspect_ai import Task, task
from inspect_ai.dataset import csv_dataset, Sample
from inspect_ai.model import ChatMessageAssistant, ChatCompletionChoice, ModelOutput
from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Target,
    accuracy,
    scorer,
    stderr,
)
from inspect_ai.solver import Generate, TaskState, solver

from agents import AgentConfig
from baseline_agents import create_agent
from run_experiment import create_client_from_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def record_to_sample(record: dict[str, Any]) -> Sample:
    """Map a CSV row to an Inspect Sample.

    Expected columns:
        problem  — the problem text (required)
        solution — the expected solution (required)
        checker  — scoring mode override (optional): "set_match", "regex"
        valid_solutions — pipe-separated alternatives for set_match (optional)
        regex_pattern   — pattern for regex checker (optional)
    """
    metadata = {}
    if record.get("checker"):
        metadata["checker"] = record["checker"].strip()
    if record.get("valid_solutions"):
        metadata["valid_solutions"] = record["valid_solutions"].strip()
    if record.get("regex_pattern"):
        metadata["regex_pattern"] = record["regex_pattern"].strip()

    return Sample(
        input=record["problem"].strip(),
        target=record["solution"].strip(),
        metadata=metadata if metadata else {},
    )


# ---------------------------------------------------------------------------
# Solver — wraps agents
# ---------------------------------------------------------------------------

def _passthrough_ground_truth(problem: str, solution: str) -> tuple[bool, str]:
    """Dummy ground truth that never short-circuits the search.

    The tree search agent uses ground truth internally to decide when to
    stop. We always return False so the agent exhausts its search and
    Inspect's scorer handles correctness.
    """
    return False, ""


@solver
def tree_search_solver(config_path: str = "configs/config.yaml", model_override: str = "", thinking: bool = False):
    """Inspect solver that runs our agent on each problem."""

    # Load config eagerly (lightweight), defer LLM client creation to first use
    config = AgentConfig.from_yaml(config_path)
    llm_holder: list = []  # lazy init on first solve() call

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        if not llm_holder:
            llm_holder.append(create_client_from_config(config, model_override=model_override or None, thinking=thinking))
        llm = llm_holder[0]

        problem = state.input_text

        # Fresh agent per problem (clean tree state)
        agent = create_agent(config, llm, _passthrough_ground_truth)
        solution = agent.solve(problem) or ""

        # Write solution into the state so the scorer can see it
        state.output = ModelOutput(
            model=config.llm_config.get("model", "unknown"),
            choices=[
                ChatCompletionChoice(
                    message=ChatMessageAssistant(content=solution),
                    stop_reason="stop",
                )
            ],
        )
        state.completed = True
        return state

    return solve


# ---------------------------------------------------------------------------
# Scorer — flexible checker dispatch
# ---------------------------------------------------------------------------

@scorer(metrics=[accuracy(), stderr()])
def problem_scorer():
    """Score solutions with per-sample checker dispatch.

    Checker modes (set via 'checker' column in CSV, default: case-insensitive):
        (default)   — case-insensitive exact match
        set_match   — match against pipe-separated alternatives in 'valid_solutions'
        regex       — match against 'regex_pattern'
    """

    async def score(state: TaskState, target: Target) -> Score:
        solution = (state.output.completion or "").strip()
        expected = target.text.strip()
        metadata = state.metadata or {}

        checker_type = metadata.get("checker", "default")

        if checker_type == "set_match":
            valid_raw = metadata.get("valid_solutions", expected)
            valid = [v.strip().lower() for v in valid_raw.split("|")]
            correct = solution.lower() in valid

        elif checker_type == "regex":
            pattern = metadata.get("regex_pattern", "")
            correct = bool(re.match(pattern, solution, re.IGNORECASE))

        else:
            # Default: case-insensitive exact match
            correct = solution.lower() == expected.lower()

        return Score(
            value=CORRECT if correct else INCORRECT,
            answer=solution,
            explanation=f"checker={checker_type}, expected={expected!r}",
        )

    return score


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------

@task
def problem_eval(
    dataset_path: str = "datasets/test_puzzles.csv",
    config_path: str = "configs/config.yaml",
    model_override: str = "",
    thinking: bool = False,
) -> Task:
    """Evaluate the tree search agent on a problem dataset."""
    return Task(
        dataset=csv_dataset(dataset_path, record_to_sample, auto_id=True),
        solver=tree_search_solver(config_path=config_path, model_override=model_override, thinking=thinking),
        scorer=problem_scorer(),
    )
