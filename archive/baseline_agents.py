"""
Baseline agents for ablation comparison against the full GoalTreeAgent.

MultiTurnAgent:
    Graph-based prompt execution with main channel and side channels.
    The prompt graph is a directed graph of nodes (prompts) and edges
    (transitions).  Edges optionally carry ``max_repeats`` to bound
    cycle traversals.  Side channels are sub-graphs that branch off the
    main channel at specified nodes: they see the full main-channel
    context but do not modify it.  Side channels can optionally check
    extracted answers against ground truth for early termination.

Other baselines retain their original linear-conversation approach:
  VanillaAgent, DefineProblemAgent, DefineProblemAndStrategyAgent,
  SingleTurnAgent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from agents import AgentConfig, LLMClient, GroundTruthChecker

logger = logging.getLogger(__name__)

# Safety limit to prevent infinite loops in graphs without max_repeats
_MAX_GRAPH_STEPS = 200


# ---------------------------------------------------------------------------
# Graph data structures
# ---------------------------------------------------------------------------

@dataclass
class GraphNode:
    """A node in the prompt graph."""
    id: str
    prompt: str


@dataclass
class GraphEdge:
    """A directed edge in the prompt graph.

    ``max_repeats``: how many times this edge can be traversed.
    ``None`` means unlimited (safe for acyclic paths; the global
    ``_MAX_GRAPH_STEPS`` prevents runaways).
    When a node has multiple outgoing edges, they are tried in list
    order — the first eligible edge is followed.  List cycle/backward
    edges before fallthrough edges for the same source node.
    """
    from_id: str
    to_id: str
    max_repeats: int | None = None


@dataclass
class SideChannelConfig:
    """A side-channel sub-graph that branches off the main channel.

    ``run_after``: IDs of main-channel nodes that trigger this side channel.
    ``check_answer``: if True, the side channel's final output is checked
        against ground truth — a correct answer terminates execution early.
    ``graph``: the side channel's own prompt graph (same structure, recursive).
    """
    id: str
    run_after: list[str]
    check_answer: bool = False
    graph: PromptGraph | None = None


@dataclass
class PromptGraph:
    """A directed graph of prompt nodes, edges, and optional side channels."""
    entry: str
    nodes: dict[str, GraphNode]
    edges: list[GraphEdge]
    side_channels: list[SideChannelConfig] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Graph parsing
# ---------------------------------------------------------------------------

def parse_graph_config(cfg: dict) -> PromptGraph:
    """Parse a ``graph`` dict from YAML into a :class:`PromptGraph`."""
    nodes: dict[str, GraphNode] = {}
    for node_id, node_cfg in cfg.get("nodes", {}).items():
        nodes[node_id] = GraphNode(id=node_id, prompt=node_cfg["prompt"])

    edges: list[GraphEdge] = []
    for e in cfg.get("edges", []):
        edges.append(GraphEdge(
            from_id=e["from"],
            to_id=e["to"],
            max_repeats=e.get("max_repeats"),
        ))

    entry = cfg.get("entry")
    if entry is None:
        raise ValueError("Graph config must specify an 'entry' node")
    if entry not in nodes:
        raise ValueError(f"Entry node {entry!r} not found in nodes")

    # Validate edge references
    for e in edges:
        for ref in (e.from_id, e.to_id):
            if ref not in nodes:
                raise ValueError(f"Edge references unknown node {ref!r}")

    side_channels: list[SideChannelConfig] = []
    for sc_cfg in cfg.get("side_channels", []):
        sc_graph = None
        if "graph" in sc_cfg:
            sc_graph = parse_graph_config(sc_cfg["graph"])
        side_channels.append(SideChannelConfig(
            id=sc_cfg["id"],
            run_after=sc_cfg.get("run_after", []),
            check_answer=sc_cfg.get("check_answer", False),
            graph=sc_graph,
        ))
        # Validate run_after references
        for ref in side_channels[-1].run_after:
            if ref not in nodes:
                raise ValueError(
                    f"Side channel {sc_cfg['id']!r} references unknown "
                    f"run_after node {ref!r}"
                )

    return PromptGraph(
        entry=entry,
        nodes=nodes,
        edges=edges,
        side_channels=side_channels,
    )


def legacy_prompts_to_graph(prompts: dict[int, str]) -> PromptGraph:
    """Convert legacy numbered prompts (prompt0, prompt1, …) into a
    simple linear :class:`PromptGraph` (no cycles, no side channels)."""
    sorted_indices = sorted(prompts)
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    for i in sorted_indices:
        nid = f"prompt{i}"
        nodes[nid] = GraphNode(id=nid, prompt=prompts[i])
    for j in range(len(sorted_indices) - 1):
        edges.append(GraphEdge(
            from_id=f"prompt{sorted_indices[j]}",
            to_id=f"prompt{sorted_indices[j + 1]}",
        ))
    entry = f"prompt{sorted_indices[0]}" if sorted_indices else None
    if entry is None:
        raise ValueError("No prompts found for legacy conversion")
    return PromptGraph(entry=entry, nodes=nodes, edges=edges)


# ---------------------------------------------------------------------------
# MultiTurnAgent — graph-based executor
# ---------------------------------------------------------------------------

class MultiTurnAgent:
    """Graph-based multi-turn agent with main channel and side channels.

    The prompt graph is specified via the ``graph`` key in the YAML config.
    Legacy numbered-prompt configs (prompt0, prompt1, …) are auto-converted
    to a simple linear graph.
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
        self.execution_tree: list[dict] = []
        self.terminated_early: bool = False
        self._problem: str = ""

    # -- helpers ------------------------------------------------------------

    def _get_latest_usage(self):
        log = getattr(self.llm, "usage_log", None)
        return log[-1] if log else None

    @staticmethod
    def _build_context(history: list[tuple[str, str]]) -> str:
        """Render conversation history as [prompt]…[response]… blocks.

        If a response carries reasoning (LLMResponse.full_text), the
        reasoning is included with <thinking> tags in the context chain.
        """
        parts = []
        for prompt_text, response_text in history:
            text = getattr(response_text, "full_text", response_text)
            parts.append(f"[prompt] {prompt_text}\n\n[response] {text}")
        return "\n\n".join(parts)

    def _call_llm(self, text: str, history: list[tuple[str, str]], *,
                   thinking: bool | str | None = None) -> str:
        """Send *text* with preceding *history* to the LLM.

        Args:
            thinking: If provided, temporarily overrides the LLM client's
                      thinking setting for this call only. Useful for side
                      channels where we want short answers without CoT.
        """
        if history:
            full_prompt = self._build_context(history) + "\n\n" + text
        else:
            full_prompt = text
        if thinking is not None and hasattr(self.llm, "thinking"):
            saved = self.llm.thinking
            self.llm.thinking = thinking
            try:
                return self.llm(full_prompt)
            finally:
                self.llm.thinking = saved
        return self.llm(full_prompt)

    def _log_call(self, label: str, purpose: str, logged_prompt: str, response: str):
        if self.exp_logger:
            self.exp_logger.log_llm_call(
                label, purpose, logged_prompt, response,
                self._get_latest_usage(),
                reasoning=getattr(response, "reasoning", ""),
            )

    # -- graph resolution ---------------------------------------------------

    def _get_graph(self) -> PromptGraph:
        if self.config.graph_config:
            return parse_graph_config(self.config.graph_config)
        if self.config.prompts:
            return legacy_prompts_to_graph(self.config.prompts)
        raise ValueError(
            "MultiTurnAgent requires either a 'graph' config or "
            "numbered prompts (prompt0, prompt1, …)"
        )

    # -- graph executor (recursive — used for main and side channels) -------

    def _execute_graph(
        self,
        graph: PromptGraph,
        inherited_history: list[tuple[str, str]],
        problem: str,
        *,
        label_prefix: str = "",
        thinking_override: bool | str | None = None,
    ) -> tuple[list[dict], str | None]:
        """Execute a prompt graph.

        Parameters
        ----------
        graph : PromptGraph
            The graph to execute.
        inherited_history : list[tuple[str, str]]
            Read-only context inherited from a parent graph (e.g. main
            channel history when executing a side channel).  The executor
            can see this history but will not modify it.
        problem : str
            Problem text — substituted for ``{problem}`` in prompts.
        label_prefix : str
            Prefix for log labels (e.g. ``"side:answer_check/"``).
        thinking_override : bool | str | None
            If not None, temporarily override the LLM's thinking setting
            for all calls in this graph. Used to disable thinking in side
            channels (answer extraction should be fast, not deep-think).

        Returns
        -------
        (own_trace, final_response)
            *own_trace*: list of dicts recording what THIS graph did
                (for JSON output — does not include inherited history).
            *final_response*: the last LLM response produced.
        """
        # Edge traversal counters: (from_id, to_id) → remaining
        edge_remaining: dict[tuple[str, str], int] = {}
        for edge in graph.edges:
            if edge.max_repeats is not None:
                edge_remaining[(edge.from_id, edge.to_id)] = edge.max_repeats

        node_visit_counts: dict[str, int] = {}
        # Full history = inherited (read-only) + own (growing)
        own_history: list[tuple[str, str]] = []
        own_trace: list[dict] = []
        last_response: str | None = None

        current_id: str | None = graph.entry
        step = 0

        while current_id is not None and step < _MAX_GRAPH_STEPS:
            step += 1
            node = graph.nodes[current_id]
            visit_num = node_visit_counts.get(current_id, 0) + 1
            node_visit_counts[current_id] = visit_num

            prompt_text = node.prompt.replace("{problem}", problem)

            # Execute node — LLM sees inherited + own history
            full_history = inherited_history + own_history
            response = self._call_llm(prompt_text, full_history,
                                       thinking=thinking_override)
            own_history.append((prompt_text, response))
            last_response = response

            # Log to .md — for inherited context we only show own portion
            if label_prefix:
                # Side channel: log only the side-channel portion
                if len(own_history) > 1:
                    logged_prompt = (
                        self._build_context(own_history[:-1])
                        + "\n\n" + prompt_text
                    )
                else:
                    logged_prompt = prompt_text
            else:
                # Main channel: log full prompt (inherited + own)
                if full_history:
                    logged_prompt = (
                        self._build_context(full_history) + "\n\n" + prompt_text
                    )
                else:
                    logged_prompt = prompt_text

            visit_label = (
                f" [visit {visit_num}]" if visit_num > 1 else ""
            )
            label = f"{label_prefix}{current_id}{visit_label}"
            self._log_call(label, label_prefix or "main_channel", logged_prompt, response)
            logger.info(f"MultiTurnAgent {label} response length: {len(response)}")

            # Build JSON trace entry (use full_text to include thinking/reasoning)
            trace_entry: dict = {
                "node_id": current_id,
                "visit": visit_num,
                "prompt": prompt_text.strip(),
                "response": getattr(response, "full_text", response),
                "branches": [],
            }

            # Run side channels triggered by this node
            for sc in graph.side_channels:
                if current_id not in sc.run_after or sc.graph is None:
                    continue

                sc_full_history = inherited_history + own_history
                sc_prefix = (
                    f"{label_prefix}side:{sc.id}/"
                    if label_prefix
                    else f"side:{sc.id}/"
                )
                sc_trace, sc_last = self._execute_graph(
                    sc.graph,
                    sc_full_history,
                    problem,
                    label_prefix=sc_prefix,
                    thinking_override=False,
                )

                branch_entry: dict = {
                    "side_channel_id": sc.id,
                    "triggered_after": current_id,
                    "triggered_after_visit": visit_num,
                    "trace": sc_trace,
                    "extracted_answer": sc_last,
                    "answer_checked": sc.check_answer,
                    "answer_correct": None,
                }

                if sc.check_answer and sc_last is not None:
                    correct, _ = self.ground_truth(self._problem, sc_last)
                    branch_entry["answer_correct"] = correct
                    logger.info(
                        f"Answer check after {current_id} [visit {visit_num}]: "
                        f"{'CORRECT' if correct else 'INCORRECT'} "
                        f"(extracted: {sc_last!r})"
                    )
                    last_response = sc_last
                    if correct:
                        logger.info(
                            f"Early termination: correct answer after "
                            f"{current_id} [visit {visit_num}]"
                        )
                        trace_entry["branches"].append(branch_entry)
                        own_trace.append(trace_entry)
                        self.terminated_early = True
                        return own_trace, last_response
                else:
                    if sc_last is not None:
                        last_response = sc_last

                trace_entry["branches"].append(branch_entry)

            own_trace.append(trace_entry)

            # Find next node — first eligible outgoing edge
            next_id: str | None = None
            for edge in graph.edges:
                if edge.from_id != current_id:
                    continue
                key = (edge.from_id, edge.to_id)
                if key in edge_remaining:
                    if edge_remaining[key] > 0:
                        edge_remaining[key] -= 1
                        next_id = edge.to_id
                        break
                    # exhausted — try next edge
                else:
                    # No max_repeats — always eligible
                    next_id = edge.to_id
                    break

            current_id = next_id

        if step >= _MAX_GRAPH_STEPS:
            logger.warning(
                f"Graph execution hit safety limit ({_MAX_GRAPH_STEPS} steps)"
            )

        return own_trace, last_response

    # -- public API ---------------------------------------------------------

    def solve(self, problem: str) -> str | None:
        self._problem = problem
        self.terminated_early = False
        graph = self._get_graph()
        self.execution_tree, last_response = self._execute_graph(
            graph, [], problem,
        )
        return last_response


# ---------------------------------------------------------------------------
# Other baseline agents (unchanged)
# ---------------------------------------------------------------------------

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
            self.exp_logger.log_llm_call("turn1", "solve", prompt, response, self._get_latest_usage(), reasoning=getattr(response, "reasoning", ""))
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
            self.exp_logger.log_llm_call("turn1", "solve", prompt, response, self._get_latest_usage(), reasoning=getattr(response, "reasoning", ""))

        # Extract solution
        extract_prompt = (
            f"[prompt] {prompt}\n\n"
            f"[response] {response}\n\n"
            f"{self.config.solution_extract_prompt}"
        )
        result = self.llm(extract_prompt)
        if self.exp_logger:
            self.exp_logger.log_llm_call("extract", "extract_solution", extract_prompt, result, self._get_latest_usage(), reasoning=getattr(result, "reasoning", ""))
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
            self.exp_logger.log_llm_call("turn1", "parent_class", turn1_prompt, turn1_response, self._get_latest_usage(), reasoning=getattr(turn1_response, "reasoning", ""))

        # Turn 2: solve
        turn2_prompt = (
            f"[prompt] {turn1_prompt}\n\n"
            f"[response] {turn1_response}\n\n"
            f"Now solve."
        )
        turn2_response = self.llm(turn2_prompt)
        logger.info(f"DefineProblemAgent turn 2 length: {len(turn2_response)}")
        if self.exp_logger:
            self.exp_logger.log_llm_call("turn2", "solve", turn2_prompt, turn2_response, self._get_latest_usage(), reasoning=getattr(turn2_response, "reasoning", ""))

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
            self.exp_logger.log_llm_call("extract", "extract_solution", extract_prompt, result, self._get_latest_usage(), reasoning=getattr(result, "reasoning", ""))
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
            self.exp_logger.log_llm_call("turn1", "parent_class", turn1_prompt, turn1_response, self._get_latest_usage(), reasoning=getattr(turn1_response, "reasoning", ""))

        # Turn 2: search strategy enumeration
        turn2_prompt = (
            f"[prompt] {turn1_prompt}\n\n"
            f"[response] {turn1_response}\n\n"
            f"{self.config.search_strategy_prompt}"
        )
        turn2_response = self.llm(turn2_prompt)
        logger.info(f"DefineProblemAndStrategyAgent turn 2 length: {len(turn2_response)}")
        if self.exp_logger:
            self.exp_logger.log_llm_call("turn2", "search_strategy", turn2_prompt, turn2_response, self._get_latest_usage(), reasoning=getattr(turn2_response, "reasoning", ""))

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
            self.exp_logger.log_llm_call("turn3", "solve", turn3_prompt, turn3_response, self._get_latest_usage(), reasoning=getattr(turn3_response, "reasoning", ""))

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
            self.exp_logger.log_llm_call("extract", "extract_solution", extract_prompt, result, self._get_latest_usage(), reasoning=getattr(result, "reasoning", ""))
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
