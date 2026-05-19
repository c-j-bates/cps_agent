"""
Tree-of-thought search agent for LLM conversation branching.

Three phases:
  Phase 1 (depth 0): Problem classification via parent_class_prompt.
      Single node, no option parsing.
  Phase 2 (depth 1): Search strategy enumeration via search_strategy_prompt.
      Produces numbered strategies. Sorted by sort_options_prompt.
  Phase 3 (depth 2+): Iterative solving via begin_solve_prompt (first turn)
      then continuation_prompt (subsequent turns). Each turn may produce a
      "choice point" with numbered options, which are sorted and branched on.

sort_options_prompt is called after every expansion that produces numbered
options (phases 2 and 3).

Each phase has its own revision prompt. Revisions propagate upward when
a node's revision budget is exhausted.

Context handling: YAML templates contain only the new instruction for each
step.  The code prepends context (via get_context_string()) when calling the
LLM.  Each Node stores the instruction that produced it, and
get_context_string() interleaves prompts and responses in the chain.
"""

from __future__ import annotations

import ast
import json
import re
import yaml
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

class Phase(Enum):
    PARENT_CLASS = "parent_class"   # depth 0
    STRATEGY = "strategy"           # depth 1
    BEGIN_SOLVE = "begin_solve"     # first solve step (under a strategy node)
    SOLVE = "solve"                 # depth 2+


def phase_for_depth(depth: int) -> Phase:
    if depth == 0:
        return Phase.PARENT_CLASS
    elif depth == 1:
        return Phase.STRATEGY
    else:
        return Phase.SOLVE


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    max_revisions: int = 3
    max_depth: int = 10

    # Agent class selector (default: tree search; baselines: "vanilla",
    # "define_problem", "define_problem_and_strategy")
    agent_class: str = ""

    # Phase 1
    parent_class_prompt: str = ""

    # Phase 2
    search_strategy_prompt: str = ""

    # Phase 3
    begin_solve_prompt: str = ""
    continuation_prompt: str = ""

    # Sorting (after any numbered-option expansion)
    sort_options_prompt: str = ""

    # Leaf / solution
    leaf_check_prompt: str = ""
    solution_extract_prompt: str = ""

    # Phase-specific revision prompts
    revision_parent_prompt: str = ""
    revision_strategy_prompt: str = ""
    revision_solve_prompt: str = ""

    # Refinement prompts (one per phase; empty = skip refinement for that phase)
    refine_parent_class_prompt: str = ""
    refine_search_strategy_prompt: str = ""
    refine_solve_step_prompt: str = ""

    # Domain hints injected into begin_solve_prompt
    domain_hints: str = ""

    # CartesianProductAgent-specific prompts
    test_hypothesis_prompt: str = ""
    revise_generator_prompt: str = ""
    refine_begin_solve_prompt: str = ""

    # CartesianProductAgent-specific settings
    # Per-phase refinement toggles (each independently controllable)
    enable_refine_parent_class: bool = True
    enable_refine_search_strategy: bool = True
    enable_refine_begin_solve: bool = False
    refine_parent_class_iterations: int = 1  # How many refinement passes on parent_class
    refine_begin_solve_iterations: int = 1  # How many refinement passes on begin_solve
    hypotheses_per_batch: int = 3      # Hypotheses to sample per iteration
    max_iterations: int = 10           # Max generate-test-revise loops

    # Tools config (e.g. code_execution settings)
    tools_config: dict = field(default_factory=dict)

    # Single-turn baseline prompt template (may contain {problem})
    prompt: str = ""

    # Multi-turn baseline: ordered prompt templates (prompt0, prompt1, …)
    # Legacy shorthand for simple linear chains — auto-converted to a graph.
    prompts: dict = field(default_factory=dict)  # {0: str, 1: str, …}

    # Graph-based prompt execution (nodes, edges, side channels)
    graph_config: dict = field(default_factory=dict)

    # Raw LLM section (kept for create_client_from_config)
    llm_config: dict = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: str) -> "AgentConfig":
        with open(path) as f:
            raw = yaml.safe_load(f)

        # Collect numbered prompts (prompt0, prompt1, …) into an int-keyed dict
        import re as _re
        prompts = {}
        for key in raw:
            m = _re.fullmatch(r"prompt(\d+)", key)
            if m:
                prompts[int(m.group(1))] = raw[key]

        return cls(
            max_revisions=raw.get("max_revisions", 3),
            max_depth=raw.get("max_depth", 10),
            agent_class=raw.get("agent_class", ""),
            parent_class_prompt=raw.get("parent_class_prompt", ""),
            search_strategy_prompt=raw.get("search_strategy_prompt", ""),
            begin_solve_prompt=raw.get("begin_solve_prompt", ""),
            continuation_prompt=raw.get("continuation_prompt", ""),
            sort_options_prompt=raw.get("sort_options_prompt", ""),
            leaf_check_prompt=raw.get("leaf_check_prompt", ""),
            solution_extract_prompt=raw.get("solution_extract_prompt", ""),
            revision_parent_prompt=raw.get("revision_parent_prompt", ""),
            revision_strategy_prompt=raw.get("revision_strategy_prompt", ""),
            revision_solve_prompt=raw.get("revision_solve_prompt", ""),
            refine_parent_class_prompt=raw.get("refine_parent_class_prompt", ""),
            refine_search_strategy_prompt=raw.get("refine_search_strategy_prompt", ""),
            refine_solve_step_prompt=raw.get("refine_solve_step_prompt", ""),
            domain_hints=raw.get("domain_hints", ""),
            test_hypothesis_prompt=raw.get("test_hypothesis_prompt", ""),
            revise_generator_prompt=raw.get("revise_generator_prompt", ""),
            refine_begin_solve_prompt=raw.get("refine_begin_solve_prompt", ""),
            enable_refine_parent_class=raw.get("enable_refine_parent_class",
                                               raw.get("enable_refinement", True)),
            enable_refine_search_strategy=raw.get("enable_refine_search_strategy",
                                                  raw.get("enable_refinement", True)),
            enable_refine_begin_solve=raw.get("enable_refine_begin_solve", False),
            refine_parent_class_iterations=raw.get("refine_parent_class_iterations", 1),
            refine_begin_solve_iterations=raw.get("refine_begin_solve_iterations", 1),
            hypotheses_per_batch=raw.get("hypotheses_per_batch", 3),
            max_iterations=raw.get("max_iterations", 10),
            tools_config=raw.get("tools", {}),
            llm_config=raw.get("llm", {}),
            prompt=raw.get("prompt", ""),
            prompts=prompts,
            graph_config=raw.get("graph", {}),
        )

    def get_revision_prompt(self, phase: "Phase") -> str:
        if phase == Phase.PARENT_CLASS:
            return self.revision_parent_prompt
        elif phase == Phase.STRATEGY:
            return self.revision_strategy_prompt
        else:  # BEGIN_SOLVE and SOLVE both use the solve revision prompt
            return self.revision_solve_prompt

    def get_refine_prompt(self, phase: "Phase") -> str:
        """Return the refinement prompt for the given phase, or '' to skip."""
        if phase == Phase.PARENT_CLASS:
            return self.refine_parent_class_prompt
        elif phase == Phase.STRATEGY:
            return self.refine_search_strategy_prompt
        else:  # BEGIN_SOLVE and SOLVE both use the solve refinement prompt
            return self.refine_solve_step_prompt


# ---------------------------------------------------------------------------
# LLM / ground-truth interfaces
# ---------------------------------------------------------------------------

class LLMClient(Protocol):
    def __call__(self, prompt: str) -> str: ...


class GroundTruthChecker(Protocol):
    def __call__(self, problem: str, solution: str) -> tuple[bool, str]: ...


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class NodeStatus(Enum):
    PENDING = auto()
    EXPANDED = auto()
    LEAF = auto()
    SOLVED = auto()
    FAILED = auto()
    REVISION_LIMIT = auto()


@dataclass
class Node:
    name: str
    content: str              # LLM response (refined if refinement was applied)
    depth: int
    phase: Phase
    prompt: str = ""          # The instruction sent to the LLM for this node
    parent: Node | None = None
    children: dict[str, Node] = field(default_factory=dict)
    status: NodeStatus = NodeStatus.PENDING
    revision_count: int = 0
    max_revisions: int = 3
    failure_reason: str = ""
    original_content: str = ""  # Pre-refinement response (empty if no refinement)
    reasoning: str = ""         # Model's reasoning/thinking content (if available)
    input_tokens: int = 0
    output_tokens: int = 0
    wall_time_seconds: float = 0.0

    @property
    def base_name(self) -> str:
        return re.sub(r"-rev\d+$", "", self.name)

    @property
    def is_revision(self) -> bool:
        return bool(re.search(r"-rev\d+$", self.name))

    def get_context_chain(self) -> list["Node"]:
        chain = []
        node: Node | None = self
        while node is not None:
            chain.append(node)
            node = node.parent
        chain.reverse()
        return chain

    def get_context_string(self) -> str:
        """Interleaved prompts and responses from root to this node.

        If a node has reasoning content, it's included with <thinking> tags.
        """
        chain = self.get_context_chain()
        parts = []
        for n in chain:
            if n.prompt:
                parts.append(f"[prompt → {n.name}] {n.prompt}")
            if n.content:
                if n.reasoning:
                    parts.append(
                        f"[{n.name}] <thinking>\n{n.reasoning}\n</thinking>\n\n{n.content}"
                    )
                else:
                    parts.append(f"[{n.name}] {n.content}")
        return "\n\n".join(parts)

    def create_revision(self, content: str) -> "Node":
        if self.parent is None:
            raise ValueError("Cannot revise root node via sibling creation")
        base = self.base_name
        existing_revs = sum(
            1 for name in self.parent.children
            if name.startswith(base + "-rev")
        )
        rev_name = f"{base}-rev{existing_revs + 1}"
        rev_node = Node(
            name=rev_name,
            content=content,
            reasoning=getattr(content, "reasoning", ""),
            depth=self.depth,
            phase=self.phase,
            parent=self.parent,
            max_revisions=self.max_revisions,
        )
        self.parent.children[rev_name] = rev_node
        logger.info(f"Created revision: {rev_name}")
        return rev_node

    def all_children_exhausted(self) -> bool:
        if not self.children:
            return False
        return all(
            c.status in (NodeStatus.FAILED, NodeStatus.REVISION_LIMIT)
            for c in self.children.values()
        )

    def get_active_children(self) -> list["Node"]:
        return [
            c for c in self.children.values()
            if c.status not in (NodeStatus.FAILED, NodeStatus.REVISION_LIMIT)
        ]

    def revision_budget_remaining(self) -> int:
        base = self.base_name
        if self.parent is None:
            return 0
        existing = sum(
            1 for name in self.parent.children
            if name.startswith(base + "-rev")
        )
        return max(0, self.max_revisions - existing)

    def __repr__(self) -> str:
        return (
            f"Node(name={self.name!r}, depth={self.depth}, "
            f"phase={self.phase.value}, status={self.status.name}, "
            f"children={len(self.children)})"
        )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class GoalTreeAgent:
    """
    Depth-first tree search agent with 3-phase prompting.

    Phase 1 (depth 0): parent_class_prompt classifies the problem.
        Single node. Becomes parent of phase 2 strategies.

    Phase 2 (depth 1): search_strategy_prompt generates search strategies.
        Options parsed, sorted via sort_options_prompt, become children.
        Each child represents a strategy to try.

    Phase 3 (depth 2+): Solving. The first turn uses begin_solve_prompt.
        Subsequent turns use continuation_prompt with {option_index} to
        select a branch at each choice point. Each LLM response is checked
        for leaf status. If not a leaf, its numbered options are parsed,
        sorted, and become children for further DFS.

    continuation_prompt is also used at depth 2 (selecting a strategy from
    phase 2) before begin_solve_prompt fires.

    Revision: each phase has its own revision prompt. When all children of
    a node exhaust their revision limits, the node itself gets revised as
    a sibling under its parent.
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
        self.logger = logger  # ExperimentLogger or None
        self.root: Node | None = None
        self.solution: str | None = None
        self.problem: str = ""

    # -- Usage tracking helpers --

    def _get_latest_usage(self):
        """Get the most recent LLMCallRecord from the LLM client, if available."""
        log = getattr(self.llm, 'usage_log', None)
        if log:
            return log[-1]
        return None

    def _log_call(self, node_name: str, purpose: str, prompt: str, response: str):
        """Record an LLM call in the experiment logger if one is set."""
        if self.logger is not None:
            self.logger.log_llm_call(
                node_name=node_name,
                purpose=purpose,
                prompt=prompt,
                response=response,
                usage=self._get_latest_usage(),
                reasoning=getattr(response, "reasoning", ""),
            )

    def _record_usage_on_node(self, node: Node, accumulate: bool = False):
        """Copy the latest LLM usage record onto a Node.

        If accumulate=True, adds to existing values (for auxiliary calls
        like sort, leaf_check, extract_solution on the same node).
        """
        usage = self._get_latest_usage()
        if usage is None:
            return
        if accumulate:
            node.input_tokens += usage.input_tokens
            node.output_tokens += usage.output_tokens
            node.wall_time_seconds += usage.wall_time_seconds
        else:
            node.input_tokens = usage.input_tokens
            node.output_tokens = usage.output_tokens
            node.wall_time_seconds = usage.wall_time_seconds

    # -- Refinement --

    def _refine_node(self, node: Node) -> None:
        """Refine the node's content using the phase-appropriate refinement prompt.

        Builds full context up to this node, inserts an "EDIT FROM HERE -->"
        marker at the start of the most recent response, and asks the LLM to
        produce a drop-in replacement.  The original response is preserved in
        node.original_content; node.content is replaced with the refined text.
        """
        refine_prompt = self.config.get_refine_prompt(node.phase)
        if not refine_prompt:
            return

        # Build context with the edit marker on the last response
        chain = node.get_context_chain()
        parts = []
        for i, n in enumerate(chain):
            if n.prompt:
                parts.append(f"[prompt → {n.name}] {n.prompt}")
            if n.content:
                if n is node:
                    # Mark the target response for editing
                    parts.append(f"[{n.name}] EDIT FROM HERE -->\n{n.content}")
                else:
                    parts.append(f"[{n.name}] {n.content}")

        context = "\n\n".join(parts)
        prompt = context + "\n\n" + refine_prompt
        refined = self.llm(prompt)

        # Store original, swap in refined
        node.original_content = node.content
        node.content = refined
        node.reasoning = getattr(refined, "reasoning", "")

        self._record_usage_on_node(node, accumulate=True)
        self._log_call(f"refine({node.name})", "refine", prompt, refined)
        logger.info(f"Refined node {node.name} (original len={len(node.original_content)}, refined len={len(refined)})")

    # -- Public API --

    def solve(self, problem: str) -> str | None:
        self.problem = problem
        self.solution = None

        # Phase 1: classify the problem (no prior context)
        instruction = self.config.parent_class_prompt.format(problem=self.problem)
        root_response = self.llm(instruction)
        self.root = Node(
            name="root",
            content=root_response,
            reasoning=getattr(root_response, "reasoning", ""),
            depth=0,
            phase=Phase.PARENT_CLASS,
            prompt=instruction,
            max_revisions=self.config.max_revisions,
        )
        self._record_usage_on_node(self.root)
        self._log_call("root", "parent_class", instruction, root_response)
        self._refine_node(self.root)
        logger.info(f"Phase 1 (parent_class) complete. Response length: {len(self.root.content)}")

        self._dfs(self.root)
        return self.solution

    # -- Core DFS --

    def _dfs(self, node: Node) -> bool:
        if self.solution is not None:
            return True

        logger.info(f"Visiting: {node}")

        if self._is_leaf(node):
            node.status = NodeStatus.LEAF
            return self._handle_leaf(node)

        # Depth limit
        if node.depth >= self.config.max_depth:
            logger.info(f"Max depth reached at {node.name}")
            node.status = NodeStatus.FAILED
            return False

        # Expand if not yet done
        if node.status == NodeStatus.PENDING:
            self._expand(node)

        # DFS into active children (already sorted by promise)
        for child in list(node.get_active_children()):
            if self._dfs(child):
                return True

        # All children exhausted — revise this node
        if node.all_children_exhausted():
            return self._handle_exhausted_node(node)

        return False

    # -- Leaf handling --

    def _handle_leaf(self, node: Node) -> bool:
        solution = self._extract_solution(node)
        correct, reason = self.ground_truth(self.problem, solution)

        if correct:
            node.status = NodeStatus.SOLVED
            self.solution = solution
            logger.info(f"SOLVED at {node.name}")
            return True

        logger.info(f"Incorrect solution at {node.name}: {reason}")
        node.failure_reason = reason
        return self._attempt_revision(node, reason)

    # -- Expansion --

    def _expand(self, node: Node) -> None:
        """
        Call the phase-appropriate LLM prompt, store the response on the node
        (or on an intermediate child when the node already has content), then
        parse numbered options from the response and attach them as children.

        Routing by current node phase:
          PARENT_CLASS (root — already has content):
              Call search_strategy_prompt, create an intermediate
              "strategy_list" child (phase=STRATEGY) to hold the response,
              then parse strategy options as its children.
          STRATEGY (empty option node under strategy_list):
              Call begin_solve_prompt, store response directly on this node
              and promote its phase to BEGIN_SOLVE, then parse solve options
              as children.
          BEGIN_SOLVE / SOLVE (empty option node):
              Call continuation_prompt, store response directly on this node,
              then parse continuation options as children.
        """
        if node.phase == Phase.PARENT_CLASS:
            # Root already has content — need an intermediate child node
            # to hold the search-strategy response.
            instruction, response = self._call_search_strategy(node)
            phase_usage = self._get_latest_usage()
            target = self._create_child_node(
                parent=node,
                name="strategy_list",
                phase=Phase.STRATEGY,
            )
            target.prompt = instruction
            target.content = response
            target.reasoning = getattr(response, "reasoning", "")
            if phase_usage:
                target.input_tokens = phase_usage.input_tokens
                target.output_tokens = phase_usage.output_tokens
                target.wall_time_seconds = phase_usage.wall_time_seconds

        elif node.phase == Phase.STRATEGY:
            # Empty option node — fill in with begin_solve output
            instruction, response = self._call_begin_solve(node)
            node.prompt = instruction
            node.content = response
            node.reasoning = getattr(response, "reasoning", "")
            node.phase = Phase.BEGIN_SOLVE
            self._record_usage_on_node(node)
            target = node

        else:
            # BEGIN_SOLVE or SOLVE — fill in with continuation output
            instruction, response = self._call_continuation(node)
            node.prompt = instruction
            node.content = response
            node.reasoning = getattr(response, "reasoning", "")
            self._record_usage_on_node(node)
            target = node

        self._refine_node(target)
        self._attach_parsed_children(target, target.content)
        node.status = NodeStatus.EXPANDED

    def _create_child_node(
        self, parent: Node, name: str, phase: Phase,
    ) -> Node:
        """Create a child node under *parent* with the given phase.

        Used for the strategy_list intermediate node (root already has
        content and cannot hold a second response).  The child is
        immediately marked EXPANDED so DFS will recurse into its children.
        """
        child = Node(
            name=name,
            content="",
            depth=parent.depth,
            phase=phase,
            parent=parent,
            max_revisions=self.config.max_revisions,
        )
        parent.children[name] = child
        child.status = NodeStatus.EXPANDED
        logger.info(
            f"Created child node {name} under {parent.name} "
            f"(depth={parent.depth}, phase={phase.value})"
        )
        return child

    def _attach_parsed_children(self, node: Node, response: str) -> None:
        """Use sort_options_prompt to determine which children to create and
        in what order.  The LLM response is treated as ground truth: whatever
        indices it returns become the children.  Falls back to a single
        option1 child if the sort response is invalid.
        """
        child_depth = node.depth + 1
        child_phase = phase_for_depth(child_depth)

        sorted_keys = self._sort_options(node, response)

        if not sorted_keys:
            # Invalid sort response — single fallback child
            child_node = Node(
                name="option1",
                content="",
                depth=child_depth,
                phase=child_phase,
                parent=node,
                max_revisions=self.config.max_revisions,
            )
            node.children["option1"] = child_node
            logger.info(f"Expanded {node.name} → 1 child (sort fallback)")
            return

        for name in sorted_keys:
            child_node = Node(
                name=name,
                content="",
                depth=child_depth,
                phase=child_phase,
                parent=node,
                max_revisions=self.config.max_revisions,
            )
            node.children[name] = child_node

        logger.info(
            f"Expanded {node.name} → {len(sorted_keys)} children "
            f"(sorted: {sorted_keys})"
        )

    # -- Phase-specific prompt calls --

    def _call_search_strategy(self, node: Node) -> tuple[str, str]:
        """Phase 1 → 2: generate search strategies.

        Returns (instruction, response) so the caller can store both on the
        response node.
        """
        instruction = self.config.search_strategy_prompt
        context = node.get_context_string()
        prompt = context + "\n\n" + instruction
        response = self.llm(prompt)
        self._log_call("strategy_list", "search_strategy", prompt, response)
        return instruction, response

    def _call_begin_solve(self, node: Node) -> tuple[str, str]:
        """Phase 2 → 3: select this strategy then begin solving. Includes solve
        instructions that apply to all remaining solve nodes.

        Returns (instruction, response) so the caller can store both on the
        response node.
        """
        instruction = self.config.begin_solve_prompt.format(
            option_index=node.name,
            domain_hints=self.config.domain_hints,
        )
        solve_context = node.get_context_string()
        solve_prompt = solve_context + "\n\n" + instruction
        response = self.llm(solve_prompt)
        self._log_call(f"solve_start({node.name})", "begin_solve", solve_prompt, response)
        return instruction, response

    def _call_continuation(self, node: Node) -> tuple[str, str]:
        """Phase 3 → 3: select the chosen option and get the LLM response.

        Sends "Proceed with option X" to the LLM.  Returns
        (instruction, response) so the caller can store both on the node.
        """
        # node.name is e.g. "option1"; strip the prefix for a clean "1"
        option_number = re.sub(r"^option", "", node.name)
        instruction = self.config.continuation_prompt.format(
            option_index=option_number,
        )
        context = node.get_context_string()
        prompt = context + "\n\n" + instruction
        response = self.llm(prompt)
        self._log_call(f"continuation({node.name})", "continuation", prompt, response)
        return instruction, response

    # -- Sorting --

    def _sort_options(self, parent: Node, expansion_response: str) -> list[str]:
        """
        Call sort_options_prompt and treat the returned index list as ground
        truth for which children to create and in what order.
        Returns a list of option names like ["option1", "option2"].
        Returns [] on any failure so the caller can fall back to a single child.
        """
        context = parent.get_context_string() + "\n\n" + expansion_response
        instruction = self.config.sort_options_prompt
        prompt = context + "\n\n" + instruction

        try:
            response = self.llm(prompt).strip()
            self._record_usage_on_node(parent, accumulate=True)
            self._log_call(f"sort({parent.name})", "sort", prompt, response)
            match = re.search(r"\[([0-9,\s]+)\]", response)
            if not match:
                logger.warning("Sort: no index list found in response, falling back to single child")
                return []
            indices = [int(x.strip()) for x in match.group(1).split(",")]
            if not indices:
                logger.warning("Sort: empty index list in response, falling back to single child")
                return []
            sorted_names = [f"option{idx}" for idx in indices]
            logger.info(f"Sorted options: {sorted_names}")
            return sorted_names

        except Exception as e:
            logger.warning(f"Sort failed ({e}), falling back to single child")
            return []

    # -- Revision logic --

    def _attempt_revision(self, node: Node, failure_reason: str) -> bool:
        if node.parent is None:
            node.status = NodeStatus.FAILED
            return False

        if node.revision_budget_remaining() <= 0:
            node.status = NodeStatus.REVISION_LIMIT
            logger.info(f"Revision limit reached for {node.name}")
            return False

        node.status = NodeStatus.FAILED
        revision_content, revision_instruction = self._generate_revision(node, failure_reason)
        rev_node = node.create_revision(revision_content)
        rev_node.prompt = revision_instruction
        self._record_usage_on_node(rev_node)
        self._refine_node(rev_node)
        return self._dfs(rev_node)

    def _handle_exhausted_node(self, node: Node) -> bool:
        reason = f"All children of {node.name} exhausted their revision limits."
        return self._attempt_revision(node, reason)

    def _generate_revision(self, node: Node, failure_reason: str) -> tuple[str, str]:
        template = self.config.get_revision_prompt(node.phase)
        # instruction = template.format(failure_reason=failure_reason)
        instruction = template  # No variables for now
        context = node.get_context_string()
        prompt = context + "\n\n" + instruction
        response = self.llm(prompt)
        self._log_call(f"revision({node.name})", "revision", prompt, response)
        return response, instruction

    # -- LLM interactions --

    def _is_leaf(self, node: Node) -> bool:
        if node.phase not in (Phase.BEGIN_SOLVE, Phase.SOLVE):
            return False
        if not node.content:
            return False
        instruction = self.config.leaf_check_prompt
        context = node.get_context_string()
        prompt = context + "\n\n" + instruction
        response = self.llm(prompt).strip()
        self._record_usage_on_node(node, accumulate=True)
        self._log_call(f"leaf_check({node.name})", "leaf_check", prompt, response)
        return response.upper().startswith("YES")

    def _extract_solution(self, node: Node) -> str:
        instruction = self.config.solution_extract_prompt
        context = node.get_context_string()
        prompt = context + "\n\n" + instruction
        response = self.llm(prompt)
        self._record_usage_on_node(node, accumulate=True)
        self._log_call(f"extract({node.name})", "extract_solution", prompt, response)
        return response

    # -- Helpers --

    @staticmethod
    # -- Visualization --

    def print_tree(self, node: Node | None = None, indent: int = 0) -> None:
        if node is None:
            node = self.root
        if node is None:
            return
        prefix = "  " * indent
        phase_tag = node.phase.value
        status = node.status.name
        rev_info = " [REV]" if node.is_revision else ""
        content_preview = node.content[:80].replace("\n", " ") if node.content else ""
        print(f"{prefix}{node.name} ({phase_tag}|{status}){rev_info}: {content_preview}...")
        for child in node.children.values():
            self.print_tree(child, indent + 1)


# ---------------------------------------------------------------------------
# CartesianProductAgent — generate-test-revise loop
# ---------------------------------------------------------------------------

class CartesianProductAgent:
    """
    Generate-test-revise agent with linear hypothesis loop.

    Phases 1-2 (parent_class, optionally search_strategy) run as a linear
    conversation. Phase 3 (begin_solve) asks the LLM to produce a variables
    dict mapping variable names to lists of possible values. The agent then
    builds a generator via itertools.product and enters a loop:

      1. Sample hypotheses_per_batch hypotheses from the product generator
      2. For each hypothesis, call LLM with test_hypothesis_prompt
      3. After the batch, call LLM with revise_generator_prompt
      4. Check termination (ground truth match or max_iterations)

    All hypothesis test results are stored in a JSON database file.
    All variable dict versions are appended to a history file.
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
        self.exp_logger = logger  # ExperimentLogger or None
        self.problem: str = ""
        self.solution: str | None = None

        # Runtime state
        self._context_parts: list[tuple[str, str]] = []
        self._hypothesis_db: list[dict] = []
        self._variables_dict: dict = {}          # Current {var: [vals]} dict
        self._variables_versions: list[dict] = []  # History of all versions
        self._hypotheses_tested: int = 0

    # -- Usage tracking / logging helpers --

    def _get_latest_usage(self):
        log = getattr(self.llm, 'usage_log', None)
        if log:
            return log[-1]
        return None

    def _log_call(self, node_name: str, purpose: str, prompt: str, response: str):
        if self.exp_logger is not None:
            self.exp_logger.log_llm_call(
                node_name=node_name,
                purpose=purpose,
                prompt=prompt,
                response=response,
                usage=self._get_latest_usage(),
                reasoning=getattr(response, "reasoning", ""),
            )

    # -- Context management --

    def _build_context_string(self) -> str:
        parts = []
        for label, text in self._context_parts:
            display = getattr(text, "full_text", text)
            parts.append(f"{label} {display}")
        return "\n\n".join(parts)

    # -- Phase runner --

    def _run_phase(
        self,
        phase_name: str,
        prompt_template: str,
        format_kwargs: dict,
        refine_prompt_text: str = "",
    ) -> str:
        """Run a single phase: format prompt, prepend context, call LLM, optionally refine.

        Args:
            phase_name: label for logging and context
            prompt_template: the prompt template (may have {fields})
            format_kwargs: substitutions for the template
            refine_prompt_text: if non-empty, the refinement prompt to apply
        Returns:
            The (possibly refined) LLM response.
        """
        instruction = prompt_template.format(**format_kwargs) if format_kwargs else prompt_template

        context = self._build_context_string()
        full_prompt = (context + "\n\n" + instruction) if context else instruction

        response = self.llm(full_prompt)
        self._log_call(phase_name, phase_name, full_prompt, response)

        if refine_prompt_text:
            # Build context with EDIT FROM HERE marker on the last response
            refine_context = self._build_context_string()
            refine_full = (
                (refine_context + "\n\n" if refine_context else "")
                + f"[{phase_name}] EDIT FROM HERE -->\n{response}"
                + "\n\n" + refine_prompt_text
            )
            refined_response = self.llm(refine_full)
            self._log_call(f"refine({phase_name})", "refine", refine_full, refined_response)
            response = refined_response

        self._context_parts.append((f"[prompt → {phase_name}]", instruction))
        self._context_parts.append((f"[{phase_name}]", response))

        return response

    # -- Experiment directory --

    def _resolve_experiment_dir(self) -> Path:
        if self.exp_logger and self.exp_logger.log_path:
            p = self.exp_logger.log_path.parent
        else:
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            p = Path("experiment_logs") / f"search_agent1_{ts}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    # -- Dict extraction --

    @staticmethod
    def _extract_python_dict(response: str, require_key: str | None = None) -> dict | None:
        """Extract a Python dict from an LLM response.

        Tries ```python blocks first (via ast.literal_eval), then falls back to
        finding bare dict literals. Optionally requires a specific key.
        """
        # Strategy 1: code blocks
        pattern = r"```(?:python|py)?\s*\n(.*?)```"
        matches = re.findall(pattern, response, re.DOTALL)

        for block in reversed(matches):
            block = block.strip()
            try:
                result = ast.literal_eval(block)
                if isinstance(result, dict):
                    if require_key is None or require_key in result:
                        return result
            except (ValueError, SyntaxError):
                pass

        # Strategy 2: find dict literal with brace matching
        brace_depth = 0
        start_idx = None
        for i, ch in enumerate(response):
            if ch == '{':
                if brace_depth == 0:
                    start_idx = i
                brace_depth += 1
            elif ch == '}':
                brace_depth -= 1
                if brace_depth == 0 and start_idx is not None:
                    candidate = response[start_idx:i + 1]
                    try:
                        result = ast.literal_eval(candidate)
                        if isinstance(result, dict):
                            if require_key is None or require_key in result:
                                return result
                    except (ValueError, SyntaxError):
                        start_idx = None
                        continue

        # Strategy 3: eval with restricted builtins for Python-isms
        for block in reversed(matches):
            block = block.strip()
            try:
                result = eval(block, {"__builtins__": {}}, {
                    "None": None, "True": True, "False": False,
                    "true": True, "false": False, "null": None,
                })
                if isinstance(result, dict):
                    if require_key is None or require_key in result:
                        return result
            except Exception:
                pass

        return None

    @staticmethod
    def _extract_variables_dict(response: str) -> dict | None:
        """Extract a variables dict {var_name: [values...]} from an LLM response.

        The variables dict maps variable names (strings) to lists of possible
        values. Validates that all values are lists.
        """
        d = CartesianProductAgent._extract_python_dict(response)
        if d is None:
            return None
        # Validate: all values should be lists
        if not all(isinstance(v, list) for v in d.values()):
            logger.warning(f"Variables dict has non-list values, coercing: {d}")
            d = {k: (v if isinstance(v, list) else [v]) for k, v in d.items()}
        if not d:
            return None
        return d

    # -- Generator via itertools.product --

    def _run_generator(self, count: int) -> list[dict]:
        """Generate up to `count` hypotheses from the variables dict via
        itertools.product, skipping already-tested hypotheses.

        Runs in a subprocess for safety (the variable values are LLM-generated).
        Returns a list of hypothesis dicts.
        """
        import itertools as _itertools

        skip = self._hypotheses_tested
        variables_json = json.dumps(self._variables_dict, default=str)

        harness = (
            "import json\n"
            "import itertools\n\n"
            f"variables = json.loads({json.dumps(variables_json)})\n"
            f"keys = list(variables.keys())\n"
            f"value_lists = [variables[k] for k in keys]\n"
            f"gen = (dict(zip(keys, combo)) for combo in itertools.product(*value_lists))\n"
            f"# Skip {skip} already-yielded hypotheses\n"
            f"for _ in range({skip}):\n"
            f"    try:\n"
            f"        next(gen)\n"
            f"    except StopIteration:\n"
            f"        break\n"
            f"results = []\n"
            f"for i, h in enumerate(gen):\n"
            f"    if i >= {count}:\n"
            f"        break\n"
            f"    results.append(h)\n"
            f"print(json.dumps(results))\n"
        )

        from code_executor import execute_python
        result = execute_python(harness, timeout=30)

        if result.return_code != 0 or result.timed_out:
            logger.error(
                f"Generator execution failed: rc={result.return_code}, "
                f"timed_out={result.timed_out}, stderr={result.stderr[:500]}"
            )
            self._log_call(
                "generator_exec", "generator_execution",
                f"[Variables dict]\n{self._variables_dict}",
                f"FAILED: {result.stderr[:500]}",
            )
            return []

        try:
            hypotheses = json.loads(result.stdout.strip())
            if not isinstance(hypotheses, list):
                logger.error(f"Generator output is not a list: {type(hypotheses)}")
                return []
            return hypotheses
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse generator output: {e}")
            return []

    # -- Variables dict file I/O --

    def _save_variables(self, exp_dir: Path) -> None:
        """Save the current variables dict to current_variables.json."""
        path = exp_dir / "current_variables.json"
        with open(path, "w") as f:
            json.dump(self._variables_dict, f, indent=2, default=str)

    def _save_variables_history(self, exp_dir: Path) -> None:
        """Save all variables dict versions to variables_history.json."""
        path = exp_dir / "variables_history.json"
        with open(path, "w") as f:
            json.dump(self._variables_versions, f, indent=2, default=str)

    # -- Hypothesis database --

    def _init_hypothesis_db(self, db_path: Path) -> None:
        if db_path.exists():
            with open(db_path) as f:
                self._hypothesis_db = json.load(f)
            logger.info(f"Loaded existing hypothesis DB with {len(self._hypothesis_db)} entries")
        else:
            self._hypothesis_db = []
            with open(db_path, "w") as f:
                json.dump([], f)
            logger.info(f"Initialized empty hypothesis DB at {db_path}")

    def _save_hypothesis_db(self, db_path: Path) -> None:
        with open(db_path, "w") as f:
            json.dump(self._hypothesis_db, f, indent=2, default=str)

    # -- Hypothesis testing --

    def _test_hypothesis(self, hypothesis: dict) -> dict:
        """Call LLM with test_hypothesis_prompt. Injects 'hypothesis' key into
        the extracted result dict so it's always present regardless of LLM output."""
        hypothesis_str = str(hypothesis)
        variables_str = json.dumps(self._variables_dict, indent=2, default=str)
        instruction = self.config.test_hypothesis_prompt.format(
            hypothesis=hypothesis_str,
            variables=variables_str,
        )

        context = self._build_context_string()
        full_prompt = context + "\n\n" + instruction

        response = self.llm(full_prompt)
        self._log_call(
            f"test_h{self._hypotheses_tested + 1}",
            "test_hypothesis",
            full_prompt,
            response,
        )

        result = self._extract_python_dict(response)
        if result is None:
            logger.warning(f"Failed to extract dict from test response for: {hypothesis}")
            result = {
                "prediction": None,
                "evidence_for": "",
                "evidence_against": "",
                "trace": "EXTRACTION_FAILED",
                "recommendations": "",
                "observations": "",
                "_raw_response": response[:2000],
            }

        # Always inject the hypothesis we fed in — authoritative source
        result["hypothesis"] = hypothesis
        return result

    def _extract_solution_from_test(self, test_result: dict) -> str | None:
        prediction = test_result.get("prediction")
        if prediction is None:
            return None
        candidate = str(prediction).strip()
        if not candidate or candidate.lower() == "none":
            return None
        return candidate

    # -- Variables revision --

    def _revise_variables(self) -> dict | None:
        """Call LLM with revise_generator_prompt, providing the current variables
        dict and ALL tested hypotheses.

        Returns the new variables dict if the LLM proposes an update, or None.
        """
        variables_str = json.dumps(self._variables_dict, indent=2, default=str)
        tested_str = json.dumps(self._hypothesis_db, indent=2, default=str)
        instruction = self.config.revise_generator_prompt.format(
            variables=variables_str,
            tested_hypotheses=tested_str,
        )

        context = self._build_context_string()
        full_prompt = context + "\n\n" + instruction

        response = self.llm(full_prompt)
        self._log_call("revise_variables", "revise_variables", full_prompt, response)

        if "no update" in response.lower():
            return None

        new_vars = self._extract_variables_dict(response)
        if new_vars is None:
            logger.warning("Revise response mentioned an update but no valid variables dict extracted")
            return None

        return new_vars

    # -- Fallback solution extraction --

    def _extract_best_solution(self) -> str | None:
        if not self._hypothesis_db:
            return None

        # Prefer hypotheses with positive evidence and no negative evidence
        strong = [
            e for e in self._hypothesis_db
            if e.get("prediction") is not None
            and e.get("evidence_for")
            and not e.get("evidence_against")
        ]
        if not strong:
            strong = [e for e in self._hypothesis_db if e.get("prediction") is not None]
        if not strong:
            return None

        candidates_str = json.dumps(strong[-5:], indent=2, default=str)
        context = self._build_context_string()
        extract_prompt = (
            context + "\n\n"
            f"Hypothesis test results:\n{candidates_str}\n\n"
            + self.config.solution_extract_prompt
        )

        result = self.llm(extract_prompt)
        self._log_call("extract_solution", "extract_solution", extract_prompt, result)
        return result.strip() if result.strip() else None

    # -- Main entry point --

    def solve(self, problem: str) -> str | None:
        self.problem = problem
        self.solution = None
        exp_dir = self._resolve_experiment_dir()

        # Initialize hypothesis database
        db_path = exp_dir / "hypothesis_db.json"
        self._init_hypothesis_db(db_path)

        # Phase 1: classify the problem
        # Run without refinement first; refinement is handled manually below
        # to support k iterations and "COPY FROM HERE:" marker extraction.
        parent_class_response = self._run_phase(
            phase_name="parent_class",
            prompt_template=self.config.parent_class_prompt,
            format_kwargs={"problem": self.problem},
            refine_prompt_text="",  # refinement handled below
        )

        # Optionally refine parent_class for k iterations.
        # The refinement prompt instructs the LLM to write "COPY FROM HERE:"
        # followed by the replacement text. We extract only the text after
        # that marker for the drop-in replacement.
        if self.config.enable_refine_parent_class and self.config.refine_parent_class_prompt:
            current_response = parent_class_response
            for refine_iter in range(self.config.refine_parent_class_iterations):
                refine_prompt_text = self.config.refine_parent_class_prompt
                # Pop the response entry so it isn't duplicated — it appears
                # below under the EDIT FROM HERE marker instead.
                last_entry = self._context_parts.pop()
                refine_context = self._build_context_string()
                self._context_parts.append(last_entry)
                refine_full = (
                    (refine_context + "\n\n" if refine_context else "")
                    + f"[parent_class] EDIT FROM HERE -->\n{current_response}"
                    + "\n\n" + refine_prompt_text
                )
                refined_response = self.llm(refine_full)
                iter_label = f"refine(parent_class, iter={refine_iter + 1})"
                self._log_call(iter_label, "refine", refine_full, refined_response)

                # Extract text after "COPY FROM HERE:" marker if present
                marker = "COPY FROM HERE:"
                marker_idx = refined_response.find(marker)
                if marker_idx != -1:
                    extracted = refined_response[marker_idx + len(marker):].strip()
                    logger.info(f"parent_class refinement {refine_iter + 1}: "
                                f"extracted {len(extracted)} chars after '{marker}'")
                    current_response = extracted
                else:
                    logger.warning(f"parent_class refinement {refine_iter + 1}: "
                                   f"'{marker}' not found, using full response as fallback")
                    current_response = refined_response

                # Update the context to use the latest refined response
                self._context_parts[-1] = ("[parent_class]", current_response)
                logger.info(f"parent_class refinement {refine_iter + 1}/"
                            f"{self.config.refine_parent_class_iterations} complete")

        logger.info("Phase 1 (parent_class) complete")

        # Phase 2: enumerate search strategies (skipped if prompt is empty)
        if self.config.search_strategy_prompt:
            refine_ss = (
                self.config.refine_search_strategy_prompt
                if self.config.enable_refine_search_strategy
                else ""
            )
            self._run_phase(
                phase_name="search_strategy",
                prompt_template=self.config.search_strategy_prompt,
                format_kwargs={},
                refine_prompt_text=refine_ss,
            )
            logger.info("Phase 2 (search_strategy) complete")
        else:
            logger.info("Phase 2 (search_strategy) skipped — no prompt configured")

        # Phase 3: begin_solve — produces initial variables dict
        # Run without refinement first so we can extract variables, generate
        # example hypotheses, and feed them into the refinement prompt.
        begin_solve_response = self._run_phase(
            phase_name="begin_solve",
            prompt_template=self.config.begin_solve_prompt,
            format_kwargs={
                "option_index": "1",
                "domain_hints": self.config.domain_hints,
            },
            refine_prompt_text="",  # refinement handled below
        )
        logger.info("Phase 3 (begin_solve) complete")

        # Extract variables dict from the raw begin_solve response
        variables = self._extract_variables_dict(begin_solve_response)
        if variables is None:
            logger.error("Failed to extract variables dict from begin_solve response")
            return None

        # Optionally refine: generate example hypotheses first so the
        # refinement agent can see what the generator actually produces.
        # Runs for refine_begin_solve_iterations passes (each sees fresh examples).
        if self.config.enable_refine_begin_solve and self.config.refine_begin_solve_prompt:
            current_response = begin_solve_response
            for refine_iter in range(self.config.refine_begin_solve_iterations):
                self._variables_dict = variables
                examples = self._run_generator(3)
                examples_str = json.dumps(examples, indent=2, default=str) if examples else "[]"
                # _hypotheses_tested is NOT incremented — these are just preview samples

                refine_prompt_text = self.config.refine_begin_solve_prompt.format(
                    example_hypotheses=examples_str,
                )
                # Pop the response entry so it isn't duplicated — it appears
                # below under the EDIT FROM HERE marker instead.
                last_entry = self._context_parts.pop()
                refine_context = self._build_context_string()
                self._context_parts.append(last_entry)
                refine_full = (
                    (refine_context + "\n\n" if refine_context else "")
                    + f"[begin_solve] EDIT FROM HERE -->\n{current_response}"
                    + "\n\n" + refine_prompt_text
                )
                refined_response = self.llm(refine_full)
                iter_label = f"refine(begin_solve, iter={refine_iter + 1})"
                self._log_call(iter_label, "refine", refine_full, refined_response)

                # Re-extract variables from the refined response
                refined_variables = self._extract_variables_dict(refined_response)
                if refined_variables is not None:
                    variables = refined_variables
                    current_response = refined_response
                    # Update the context to use the latest refined response
                    self._context_parts[-1] = ("[begin_solve]", current_response)
                    logger.info(f"begin_solve refinement {refine_iter + 1}/{self.config.refine_begin_solve_iterations} "
                                f"succeeded: {list(variables.keys())}")
                else:
                    logger.warning(f"begin_solve refinement {refine_iter + 1} failed to extract variables; stopping refinement")
                    break

        self._variables_dict = variables
        self._variables_versions.append(variables)
        self._save_variables(exp_dir)
        self._save_variables_history(exp_dir)
        logger.info(f"Initial variables dict: {list(variables.keys())} "
                     f"({sum(len(v) for v in variables.values())} total values)")

        # Main generate-test-revise loop
        for iteration in range(self.config.max_iterations):
            logger.info(f"=== Iteration {iteration + 1}/{self.config.max_iterations} ===")

            # (a) Sample hypotheses from product of variable lists
            hypotheses = self._run_generator(self.config.hypotheses_per_batch)

            if not hypotheses:
                logger.warning(f"Generator produced 0 hypotheses at iteration {iteration + 1}")
            else:
                # (b) Test each hypothesis
                for h_idx, hypothesis in enumerate(hypotheses):
                    logger.info(f"  Testing hypothesis {h_idx + 1}/{len(hypotheses)}")

                    test_result = self._test_hypothesis(hypothesis)
                    self._hypothesis_db.append(test_result)
                    self._save_hypothesis_db(db_path)
                    self._hypotheses_tested += 1

                    # Check ground truth
                    solution_candidate = self._extract_solution_from_test(test_result)
                    if solution_candidate:
                        correct, reason = self.ground_truth(self.problem, solution_candidate)
                        if correct:
                            self.solution = solution_candidate
                            logger.info(f"SOLVED at iteration {iteration + 1}, hypothesis {h_idx + 1}")
                            return self.solution

            # (c) Revise variables (using ALL accumulated results)
            revised_vars = self._revise_variables()
            if revised_vars is not None:
                self._variables_dict = revised_vars
                self._variables_versions.append(revised_vars)
                # Reset skip counter — new variable space produces different sequence
                self._hypotheses_tested = 0
                self._save_variables(exp_dir)
                self._save_variables_history(exp_dir)
                logger.info(f"Variables updated (version {len(self._variables_versions)}): "
                            f"{list(revised_vars.keys())}")
            else:
                logger.info("No variables update this iteration")

        # Exhausted iterations
        logger.info("Max iterations reached without ground truth match")
        return self._extract_best_solution()
