"""
Baseline agents for ablation comparison against the full GoalTreeAgent.

All three baselines run a simple linear conversation (no tree search)
and share the same constructor signature and solve() API as GoalTreeAgent,
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
from agents import AgentConfig, LLMClient, GroundTruthChecker

logger = logging.getLogger(__name__)


class MultiTurnAgent:
    """Multi-turn baseline driven by numbered prompts (prompt0, prompt1, …).

    Conversation flow controlled by ``loop_start``:
      - Prompts before ``loop_start`` run once each (preamble). Each prompt
        and its response are appended to the conversation context.
      - The prompt at ``loop_start`` is repeated up to ``max_repeats`` times.
        Each call and its response are added to the context.
      - If one more prompt exists after ``loop_start`` it is called after
        every loop iteration as a side-channel extraction: the LLM sees
        the current context plus this prompt, but neither the prompt nor
        its response are added to the main context for future loop calls.
      - If ``loop_start`` is omitted all prompts run once (no loop).

    Only prompt0 may contain ``{problem}``; all others are literal.
    """

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

    @staticmethod
    def _build_context(history: list[tuple[str, str]]) -> str:
        """Render conversation history as [prompt]…[response]… blocks."""
        parts = []
        for prompt_text, response_text in history:
            parts.append(f"[prompt] {prompt_text}\n\n[response] {response_text}")
        return "\n\n".join(parts)

    def _call_prompt(self, idx: int, text: str, history, tag: str = ""):
        """Send a prompt with conversation history, log it, return response."""
        if history:
            full_prompt = self._build_context(history) + "\n\n" + text
        else:
            full_prompt = text
        response = self.llm(full_prompt)
        label = tag or f"prompt{idx}"
        logger.info(f"MultiTurnAgent {label} response length: {len(response)}")
        if self.exp_logger:
            self.exp_logger.log_llm_call(label, label, full_prompt, response, self._get_latest_usage())
        return response

    def solve(self, problem: str) -> str | None:
        prompts = self.config.prompts  # {0: str, 1: str, …}
        if not prompts:
            raise ValueError("MultiTurnAgent requires at least prompt0 in config")

        sorted_indices = sorted(prompts)
        loop_start = self.config.loop_start  # None means no loop
        max_repeats = self.config.max_repeats
        history: list[tuple[str, str]] = []
        last_response: str | None = None

        # Determine which indices are preamble, loop body, and side-channel
        if loop_start is not None:
            preamble_indices = [i for i in sorted_indices if i < loop_start]
            loop_idx = loop_start
            side_idx = loop_start + 1 if (loop_start + 1) in prompts else None
        else:
            preamble_indices = sorted_indices
            loop_idx = None
            side_idx = None

        # --- Preamble: sequential, non-looping ---
        for i in preamble_indices:
            text = prompts[i].format(problem=problem) if i == 0 else prompts[i]
            response = self._call_prompt(i, text, history)
            history.append((text, response))
            last_response = response

        # --- Loop ---
        if loop_idx is not None and loop_idx in prompts:
            for k in range(max_repeats):
                # Loop body — added to context
                text_loop = prompts[loop_idx]
                response_loop = self._call_prompt(
                    loop_idx, text_loop, history,
                    tag=f"prompt{loop_idx}_iter{k}",
                )
                history.append((text_loop, response_loop))
                last_response = response_loop

                # Side-channel extraction — NOT added to context
                if side_idx is not None:
                    text_side = prompts[side_idx]
                    response_side = self._call_prompt(
                        side_idx, text_side, history,
                        tag=f"prompt{side_idx}_extract_iter{k}",
                    )
                    last_response = response_side

        return last_response


class SingleTurnAgent:
    """True one-turn baseline: send the prompt, return the raw response.

    Uses the `prompt` field from the config (with {problem} substitution).
    No solution extraction step — the raw LLM response is the result.
    """

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
        prompt = self.config.prompt.format(problem=problem)
        response = self.llm(prompt)
        logger.info(f"SingleTurnAgent response length: {len(response)}")
        if self.exp_logger:
            self.exp_logger.log_llm_call("turn1", "solve", prompt, response, self._get_latest_usage())
        return response


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
    "baseline-distinct-ideas": SingleTurnAgent,
    "baseline-think-step-by-step": SingleTurnAgent,
    "baseline": SingleTurnAgent,
    "multi_turn": MultiTurnAgent,
}


def create_agent(
    config: AgentConfig,
    llm: LLMClient,
    ground_truth: GroundTruthChecker,
    exp_logger=None,
):
    """Factory: return the right agent class based on config.agent_class.

    Falls back to GoalTreeAgent when agent_class is empty or unrecognized.
    """
    from agents import GoalTreeAgent, CartesianProductAgent

    if config.agent_class in BASELINE_AGENTS:
        cls = BASELINE_AGENTS[config.agent_class]
        logger.info(f"Using baseline agent: {config.agent_class}")
        return cls(config, llm, ground_truth, logger=exp_logger)

    if config.agent_class == "cartesian_product":
        logger.info("Using CartesianProductAgent")
        return CartesianProductAgent(config, llm, ground_truth, logger=exp_logger)
    
    if config.agent_class == "goal_tree":
        logger.info("Using GoalTreeAgent")
        return GoalTreeAgent(config, llm, ground_truth, logger=exp_logger)

    logger.warning(
        f"Unknown agent_class {config.agent_class!r}, "
        f"falling back to GoalTreeAgent"
    )

    return GoalTreeAgent(config, llm, ground_truth, logger=exp_logger)
