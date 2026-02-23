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

import re
import yaml
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
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

    # Tools config (e.g. code_execution settings)
    tools_config: dict = field(default_factory=dict)

    # Raw LLM section (kept for create_client_from_config)
    llm_config: dict = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: str) -> "AgentConfig":
        with open(path) as f:
            raw = yaml.safe_load(f)
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
            tools_config=raw.get("tools", {}),
            llm_config=raw.get("llm", {}),
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
        """Interleaved prompts and responses from root to this node."""
        chain = self.get_context_chain()
        parts = []
        for n in chain:
            if n.prompt:
                parts.append(f"[prompt → {n.name}] {n.prompt}")
            if n.content:
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

class SearchAgent:
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
            if phase_usage:
                target.input_tokens = phase_usage.input_tokens
                target.output_tokens = phase_usage.output_tokens
                target.wall_time_seconds = phase_usage.wall_time_seconds

        elif node.phase == Phase.STRATEGY:
            # Empty option node — fill in with begin_solve output
            instruction, response = self._call_begin_solve(node)
            node.prompt = instruction
            node.content = response
            node.phase = Phase.BEGIN_SOLVE
            self._record_usage_on_node(node)
            target = node

        else:
            # BEGIN_SOLVE or SOLVE — fill in with continuation output
            instruction, response = self._call_continuation(node)
            node.prompt = instruction
            node.content = response
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
