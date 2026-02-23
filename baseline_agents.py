"""
Baseline agents for ablation comparison against the full SearchAgent.

All three baselines run a simple linear conversation (no tree search)
and share the same constructor signature and solve() API as SearchAgent,
so they plug directly into run_experiment.py and eval_task.py.

Baselines (cumulative ablations of the full agent):
  VanillaAgent:
      Single prompt: "Problem: {problem}". No scaffolding at all.

  DefineProblemAgent:
      Turn 1: parent_class_prompt (problem classification / variable enumeration).
      Turn 2: "Now solve."

  DefineProblemAndStrategyAgent:
      Turn 1: parent_class_prompt.
      Turn 2: search_strategy_prompt (enumerate search procedures).
      Turn 3: "Now solve."
"""

from __future__ import annotations

import logging
from tree_agent import AgentConfig, LLMClient, GroundTruthChecker

logger = logging.getLogger(__name__)


class VanillaAgent:
    """Single-shot baseline: just sends the problem, extracts the answer."""

    def __init__(
        self,
        config: AgentConfig,
        llm: LLMClient,
        ground_truth: GroundTruthChecker,
        logger=None,
    ):
        self.config = config
        self.llm = llm
        self.ground_truth = ground_truth
        self.exp_logger = logger

    def _get_latest_usage(self):
        log = getattr(self.llm, 'usage_log', None)
        if log:
            return log[-1]
        return None

    def solve(self, problem: str) -> str | None:
        prompt = f"Problem: {problem}"
        response = self.llm(prompt)
        logger.info(f"VanillaAgent response length: {len(response)}")
        if self.exp_logger:
            self.exp_logger.log_llm_call("turn1", "solve", prompt, response, self._get_latest_usage())

        # Extract solution
        extract_prompt = (
            f"[prompt] {prompt}\n\n"
            f"[response] {response}\n\n"
            f"{self.config.solution_extract_prompt}"
        )
        result = self.llm(extract_prompt)
        if self.exp_logger:
            self.exp_logger.log_llm_call("extract", "extract_solution", extract_prompt, result, self._get_latest_usage())
        return result


class DefineProblemAgent:
    """Two-turn baseline: classify the problem, then solve."""

    def __init__(
        self,
        config: AgentConfig,
        llm: LLMClient,
        ground_truth: GroundTruthChecker,
        logger=None,
    ):
        self.config = config
        self.llm = llm
        self.ground_truth = ground_truth
        self.exp_logger = logger

    def _get_latest_usage(self):
        log = getattr(self.llm, 'usage_log', None)
        if log:
            return log[-1]
        return None

    def solve(self, problem: str) -> str | None:
        # Turn 1: problem classification
        turn1_prompt = self.config.parent_class_prompt.format(problem=problem)
        turn1_response = self.llm(turn1_prompt)
        logger.info(f"DefineProblemAgent turn 1 length: {len(turn1_response)}")
        if self.exp_logger:
            self.exp_logger.log_llm_call("turn1", "parent_class", turn1_prompt, turn1_response, self._get_latest_usage())

        # Turn 2: solve
        turn2_prompt = (
            f"[prompt] {turn1_prompt}\n\n"
            f"[response] {turn1_response}\n\n"
            f"Now solve."
        )
        turn2_response = self.llm(turn2_prompt)
        logger.info(f"DefineProblemAgent turn 2 length: {len(turn2_response)}")
        if self.exp_logger:
            self.exp_logger.log_llm_call("turn2", "solve", turn2_prompt, turn2_response, self._get_latest_usage())

        # Extract solution
        extract_prompt = (
            f"[prompt] {turn1_prompt}\n\n"
            f"[response] {turn1_response}\n\n"
            f"[prompt] Now solve.\n\n"
            f"[response] {turn2_response}\n\n"
            f"{self.config.solution_extract_prompt}"
        )
        result = self.llm(extract_prompt)
        if self.exp_logger:
            self.exp_logger.log_llm_call("extract", "extract_solution", extract_prompt, result, self._get_latest_usage())
        return result


class DefineProblemAndStrategyAgent:
    """Three-turn baseline: classify, enumerate strategies, then solve."""

    def __init__(
        self,
        config: AgentConfig,
        llm: LLMClient,
        ground_truth: GroundTruthChecker,
        logger=None,
    ):
        self.config = config
        self.llm = llm
        self.ground_truth = ground_truth
        self.exp_logger = logger

    def _get_latest_usage(self):
        log = getattr(self.llm, 'usage_log', None)
        if log:
            return log[-1]
        return None

    def solve(self, problem: str) -> str | None:
        # Turn 1: problem classification
        turn1_prompt = self.config.parent_class_prompt.format(problem=problem)
        turn1_response = self.llm(turn1_prompt)
        logger.info(f"DefineProblemAndStrategyAgent turn 1 length: {len(turn1_response)}")
        if self.exp_logger:
            self.exp_logger.log_llm_call("turn1", "parent_class", turn1_prompt, turn1_response, self._get_latest_usage())

        # Turn 2: search strategy enumeration
        turn2_prompt = (
            f"[prompt] {turn1_prompt}\n\n"
            f"[response] {turn1_response}\n\n"
            f"{self.config.search_strategy_prompt}"
        )
        turn2_response = self.llm(turn2_prompt)
        logger.info(f"DefineProblemAndStrategyAgent turn 2 length: {len(turn2_response)}")
        if self.exp_logger:
            self.exp_logger.log_llm_call("turn2", "search_strategy", turn2_prompt, turn2_response, self._get_latest_usage())

        # Turn 3: solve
        turn3_prompt = (
            f"[prompt] {turn1_prompt}\n\n"
            f"[response] {turn1_response}\n\n"
            f"[prompt] {self.config.search_strategy_prompt}\n\n"
            f"[response] {turn2_response}\n\n"
            f"Now solve."
        )
        turn3_response = self.llm(turn3_prompt)
        logger.info(f"DefineProblemAndStrategyAgent turn 3 length: {len(turn3_response)}")
        if self.exp_logger:
            self.exp_logger.log_llm_call("turn3", "solve", turn3_prompt, turn3_response, self._get_latest_usage())

        # Extract solution
        extract_prompt = (
            f"[prompt] {turn1_prompt}\n\n"
            f"[response] {turn1_response}\n\n"
            f"[prompt] {self.config.search_strategy_prompt}\n\n"
            f"[response] {turn2_response}\n\n"
            f"[prompt] Now solve.\n\n"
            f"[response] {turn3_response}\n\n"
            f"{self.config.solution_extract_prompt}"
        )
        result = self.llm(extract_prompt)
        if self.exp_logger:
            self.exp_logger.log_llm_call("extract", "extract_solution", extract_prompt, result, self._get_latest_usage())
        return result


# Map of baseline names to classes for use by run_experiment / eval_task
BASELINE_AGENTS = {
    "vanilla": VanillaAgent,
    "define_problem": DefineProblemAgent,
    "define_problem_and_strategy": DefineProblemAndStrategyAgent,
}


def create_agent(
    config: AgentConfig,
    llm: LLMClient,
    ground_truth: GroundTruthChecker,
    exp_logger=None,
):
    """Factory: return the right agent class based on config.agent_class.

    Falls back to SearchAgent when agent_class is empty or unrecognized.
    """
    from tree_agent import SearchAgent

    if config.agent_class in BASELINE_AGENTS:
        cls = BASELINE_AGENTS[config.agent_class]
        logger.info(f"Using baseline agent: {config.agent_class}")
        return cls(config, llm, ground_truth, logger=exp_logger)

    if config.agent_class and config.agent_class not in ("", "tree_search"):
        logger.warning(
            f"Unknown agent_class {config.agent_class!r}, "
            f"falling back to SearchAgent"
        )

    return SearchAgent(config, llm, ground_truth, logger=exp_logger)
