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
import traceback
from datetime import datetime
from pathlib import Path
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

from agents import AgentConfig, create_agent, make_ground_truth
from experiment_logger import ExperimentLogger
from run_experiment import create_client_from_config

logger = logging.getLogger(__name__)

# Errors that are genuinely transient and worth retrying on the next sample.
# Everything else (BadRequestError, AuthenticationError, code bugs, etc.)
# indicates a persistent problem and should fail the whole eval immediately.
try:
    from openai import APIConnectionError, APITimeoutError, RateLimitError
    _TRANSIENT_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError)
except ImportError:
    _TRANSIENT_ERRORS = ()


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

@solver
def tree_search_solver(
    config_path: str = "configs/config.yaml",
    model_override: str = "",
    thinking: bool | str = False,
    provider_override: str = "",
    base_url: str = "",
    experiment_log_dir: str = "",
    timeout: float | None = None,
    max_tokens: int | None = None,
    dataset_path: str = "",
):
    """Inspect solver that runs our agent on each problem."""

    # Load config eagerly (lightweight), defer LLM client creation to first use
    config = AgentConfig.from_yaml(config_path)
    llm_holder: list = []  # lazy init on first solve() call

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        problem = state.input_text
        problem_id = str(state.sample_id or state.epoch)
        solution = ""
        exp_logger = None
        usage_log = []
        usage_start = 0
        agent = None

        try:
            if not llm_holder:
                llm_holder.append(create_client_from_config(
                    config,
                    model_override=model_override or None,
                    thinking=thinking,
                    provider_override=provider_override or None,
                    base_url=base_url or None,
                    timeout=timeout,
                    max_tokens_override=max_tokens,
                ))
            llm = llm_holder[0]

            # Set up experiment logger for conversation .md / .json output
            if experiment_log_dir:
                effective_model = model_override or config.llm_config.get("model", "unknown")
                effective_provider = provider_override or config.llm_config.get("provider", "unknown")
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_id = problem_id.replace("/", "_").replace(" ", "_")
                run_dir = Path(experiment_log_dir) / f"{timestamp}_{safe_id}"
                exp_logger = ExperimentLogger(log_dir=str(run_dir))
                exp_logger.initialize(
                    problem_id=problem_id,
                    problem=problem,
                    config=config,
                    provider=effective_provider,
                    model=effective_model,
                    config_path=config_path,
                    dataset_path=dataset_path,
                    thinking=thinking,
                )

            # Snapshot usage_log length so we can measure per-sample tokens
            usage_log = getattr(llm, "usage_log", [])
            usage_start = len(usage_log)

            # Build ground truth checker from the sample's target so the agent
            # can stop early when it finds the correct answer
            target = state.target.text if state.target else ""
            ground_truth = make_ground_truth(target)

            # Fresh agent per problem (clean tree state)
            agent = create_agent(config, llm, ground_truth, exp_logger=exp_logger)
            solution = agent.solve(problem) or ""

        except (KeyboardInterrupt, SystemExit):
            raise
        except _TRANSIENT_ERRORS:
            # Transient / API errors (timeouts, rate limits, connection) —
            # log and continue to next sample
            tb = traceback.format_exc()
            logger.error(f"Solver error for sample {problem_id}:\n{tb}")
            if state.metadata is None:
                state.metadata = {}
            state.metadata["solver_error"] = tb
        except Exception:
            # Everything else (code bugs, bad request params, auth errors,
            # invalid model names) — fail fast so the eval stops immediately
            logger.error(f"Fatal error in solver for sample {problem_id}:\n"
                         f"{traceback.format_exc()}")
            raise

        # Finalize experiment logger (outside try so it runs even after error)
        try:
            if exp_logger:
                if agent and hasattr(agent, "root"):
                    exp_logger.log_tree(agent.root)
                sample_usage = usage_log[usage_start:]
                exp_logger.log_summary(solution, sample_usage)
                if agent and hasattr(agent, "execution_tree") and agent.execution_tree:
                    json_tree = {
                        "metadata": {
                            "problem_id": problem_id,
                            "problem": problem,
                            "config_path": config_path,
                            "provider": provider_override or config.llm_config.get("provider", ""),
                            "model": model_override or config.llm_config.get("model", ""),
                            "thinking": thinking if thinking else False,
                            "timestamp": datetime.now().isoformat(),
                        },
                        "execution": agent.execution_tree,
                        "final_answer": solution,
                        "terminated_early": getattr(agent, "terminated_early", False),
                    }
                    exp_logger.log_json_tree(json_tree)
                exp_logger.finalize()
        except Exception:
            logger.error(f"Error finalizing experiment logger: {traceback.format_exc()}")

        # Compute per-sample token usage
        sample_usage = usage_log[usage_start:]
        sample_input_tokens = sum(r.input_tokens for r in sample_usage)
        sample_output_tokens = sum(r.output_tokens for r in sample_usage)
        sample_wall_time = sum(r.wall_time_seconds for r in sample_usage)
        sample_llm_calls = len(sample_usage)

        # Count graph iterations (for multi-turn agents)
        execution_tree = getattr(agent, "execution_tree", []) if agent else []
        graph_steps = len(execution_tree) if execution_tree else sample_llm_calls
        terminated_early = getattr(agent, "terminated_early", False) if agent else False

        # Store per-sample metrics in state metadata for the scorer
        if state.metadata is None:
            state.metadata = {}
        state.metadata["input_tokens"] = sample_input_tokens
        state.metadata["output_tokens"] = sample_output_tokens
        state.metadata["total_tokens"] = sample_input_tokens + sample_output_tokens
        state.metadata["wall_time_seconds"] = round(sample_wall_time, 2)
        state.metadata["llm_calls"] = sample_llm_calls
        state.metadata["graph_steps"] = graph_steps
        state.metadata["terminated_early"] = terminated_early

        # Write solution into the state so the scorer can see it
        effective_model = model_override or config.llm_config.get("model", "unknown")
        state.output = ModelOutput(
            model=effective_model,
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
    """Score solutions using the same make_ground_truth checker the agent uses.

    This ensures the scorer and the agent's internal answer check always agree.
    """

    async def score(state: TaskState, target: Target) -> Score:
        solution = (state.output.completion or "").strip()
        expected = target.text.strip()

        checker = make_ground_truth(expected)
        correct, detail = checker(state.input_text, solution)

        return Score(
            value=CORRECT if correct else INCORRECT,
            answer=solution,
            explanation=detail or f"expected={expected!r}",
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
    thinking: bool | str = False,
    provider_override: str = "",
    base_url: str = "",
    experiment_log_dir: str = "",
    timeout: float | None = None,
) -> Task:
    """Evaluate the tree search agent on a problem dataset."""
    return Task(
        dataset=csv_dataset(dataset_path, record_to_sample, auto_id=True),
        solver=tree_search_solver(
            config_path=config_path,
            model_override=model_override,
            thinking=thinking,
            provider_override=provider_override,
            base_url=base_url,
            experiment_log_dir=experiment_log_dir,
            timeout=timeout,
            dataset_path=dataset_path,
        ),
        scorer=problem_scorer(),
    )
