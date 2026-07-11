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

import json
import logging
import os
import re as _re
import shutil
import subprocess
import tempfile
import yaml
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from llm_clients import LLMClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ground-truth interface
# ---------------------------------------------------------------------------

class GroundTruthChecker(Protocol):
    def __call__(self, problem: str, solution: str) -> tuple[bool, str]: ...


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    agent_class: str = ""

    # Tools config (e.g. code_execution settings)
    tools_config: dict = field(default_factory=dict)

    # Single-turn baseline prompt template (may contain {problem})
    prompt: str = ""

    # Multi-turn baseline: ordered prompt templates (prompt0, prompt1, …)
    prompts: dict = field(default_factory=dict)

    # Graph-based prompt execution (nodes, edges, side channels)
    graph_config: dict = field(default_factory=dict)

    # Raw LLM section (kept for create_client_from_config)
    llm_config: dict = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: str) -> "AgentConfig":
        with open(path) as f:
            raw = yaml.safe_load(f)

        # Collect numbered prompts (prompt0, prompt1, …) into an int-keyed dict
        prompts = {}
        for key in raw:
            m = _re.fullmatch(r"prompt(\d+)", key)
            if m:
                prompts[int(m.group(1))] = raw[key]

        return cls(
            agent_class=raw.get("agent_class", ""),
            tools_config=raw.get("tools", {}),
            llm_config=raw.get("llm", {}),
            prompt=raw.get("prompt", ""),
            prompts=prompts,
            graph_config=raw.get("graph", {}),
        )


# ---------------------------------------------------------------------------
# Ground-truth checker builder
# ---------------------------------------------------------------------------

def _is_checker_function(solution: str) -> bool:
    """True if the solution field is a Python checker function."""
    return isinstance(solution, str) and solution.strip().startswith("def check(")


def _is_codeforces_blob(solution_spec: str) -> bool:
    """True if the solution field is a codeforces test-blob: JSON with inline
    'tests' (a list) or a 'tests_file' sidecar reference (a string)."""
    if not isinstance(solution_spec, str):
        return False
    s = solution_spec.strip()
    if not (s.startswith("{") and ('"tests"' in s or '"tests_file"' in s)):
        return False
    try:
        d = json.loads(s)
        return isinstance(d, dict) and (
            isinstance(d.get("tests"), list) or isinstance(d.get("tests_file"), str)
        )
    except Exception:
        return False


_CPP_BLOCK_RE = _re.compile(r"```(?:cpp|c\+\+|c)?\s*\n(.*?)```", _re.DOTALL)


def _extract_cpp(solution: str) -> str | None:
    """Pull the first ```cpp ... ``` block from a solution; fall back to the
    whole text if it looks like C++ source."""
    if not solution:
        return None
    m = _CPP_BLOCK_RE.search(solution)
    if m:
        return m.group(1).strip()
    s = solution.strip()
    if s.startswith("#include") or "int main" in s[:400]:
        return s
    return None


def _resolve_sidecar(path: str) -> str | None:
    """Resolve a sidecar test-file path: try as given (cwd-relative / absolute),
    else relative to this module's directory. Returns the existing path or None."""
    if not path:
        return None
    if os.path.isfile(path):
        return path
    alt = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    return alt if os.path.isfile(alt) else None


def _make_codeforces_checker(blob_str: str):
    """Build a checker that compiles the solver's C++ and runs it against the
    held-out tests for the problem.

    Blob fields (any of): inline tests=[{input,output}] + checker; OR a sidecar
    reference tests_file (committed official tests) and optional gen_tests_file
    (gitignored, regenerable generated tests appended for stronger grading) — each
    a JSON file {tests:[...], checker?}. Plus time_limit (s), memory_limit (MB; not
    enforced locally). A test passes if the program's output matches token-wise, OR
    — for multi-answer problems — the special-judge checker accepts it. The SAME
    checker is used by the agent's early-termination and by problem_scorer, so the
    two always agree.
    """
    data = json.loads(blob_str)
    tests = list(data.get("tests", []) or [])
    checker_src = (data.get("checker") or "").strip()
    # Sidecar files keep the CSV small (tests/checkers live outside it).
    tf = data.get("tests_file")
    if tf:
        p = _resolve_sidecar(tf)
        if p:
            with open(p) as fh:
                side = json.load(fh)
            tests = list(side.get("tests", []) or [])
            if not checker_src:
                checker_src = (side.get("checker") or "").strip()
    gtf = data.get("gen_tests_file")
    if gtf:
        p = _resolve_sidecar(gtf)
        if p:
            with open(p) as fh:
                side = json.load(fh)
            tests = tests + list(side.get("tests", []) or [])
    time_limit = float(data.get("time_limit", 2.0) or 2.0)
    per_test_timeout = max(10.0, time_limit * 6.0)  # generous: local hardware != judge

    def checker(problem: str, solution: str) -> tuple[bool, str]:
        # The detail string is judge-style feedback (verdict + first failing test +
        # how many passed), matching what a real submitter sees. It is surfaced to the
        # solver on its next turn (see the {feedback} substitution in _execute_graph).
        code = _extract_cpp(solution)
        if code is None:
            return False, "No C++ code block found in your submission."
        if not tests:
            return False, "no tests available in target blob"
        workdir = tempfile.mkdtemp(prefix="cf_chk_")
        try:
            src = os.path.join(workdir, "main.cpp")
            exe = os.path.join(workdir, "main")
            with open(src, "w") as f:
                f.write(code)
            try:
                cp = subprocess.run(["g++", "-O2", "-std=c++17", "-o", exe, src],
                                    capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                return False, "Compilation error: the compiler timed out (60s)."
            if cp.returncode != 0:
                return False, f"Compilation error:\n{cp.stderr.strip()[:1500]}"
            chk_path = None
            if checker_src:
                chk_path = os.path.join(workdir, "checker.py")
                with open(chk_path, "w") as f:
                    f.write(checker_src)
            n = len(tests)
            for j, t in enumerate(tests):
                inp = t.get("input", "")
                exp = t.get("output", "")
                try:
                    run = subprocess.run([exe], input=inp, capture_output=True,
                                         text=True, timeout=per_test_timeout)
                except subprocess.TimeoutExpired:
                    return False, (f"Time limit exceeded on test {j+1} "
                                   f"(passed {j} of {n}); your program exceeded the time budget.")
                if run.returncode != 0:
                    why = (f"killed by signal {-run.returncode}" if run.returncode < 0
                           else f"exit code {run.returncode}")
                    err = (run.stderr or "").strip()
                    tail = f"; stderr: {err[-300:]}" if err else ""
                    return False, (f"Runtime error on test {j+1} "
                                   f"(passed {j} of {n}): {why}{tail}")
                if run.stdout.split() == exp.split():
                    continue
                # token mismatch -> try the special-judge checker (multi-answer problems)
                if chk_path:
                    try:
                        ci = os.path.join(workdir, "in.txt")
                        co = os.path.join(workdir, "cor.txt")
                        cs = os.path.join(workdir, "out.txt")
                        for pth, c in ((ci, inp), (co, exp), (cs, run.stdout)):
                            with open(pth, "w") as f:
                                f.write(c)
                        cr = subprocess.run(["python3", chk_path, ci, co, cs],
                                            capture_output=True, text=True, timeout=30)
                        if cr.stdout.strip().startswith("1"):
                            continue
                    except Exception:
                        pass
                return False, f"Wrong answer on test {j+1} (passed {j} of {n})."
            return True, f"Accepted — passed all {n} tests."
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    return checker


def make_ground_truth(solution_spec: str):
    """Build a GroundTruthChecker from a solution spec.

    Returns a callable(problem, solution) -> (bool, str).
    """
    if _is_codeforces_blob(solution_spec):
        return _make_codeforces_checker(solution_spec)

    if _is_checker_function(solution_spec):
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

    expected = str(solution_spec).strip()

    def _literal_checker(problem: str, solution: str) -> tuple[bool, str]:
        if solution.strip().lower() == expected.lower():
            return True, ""
        return False, f"Expected {expected!r}, got: {solution.strip()[:80]!r}"

    return _literal_checker



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
        # Judge feedback on the most recent checked submission — substituted for
        # {feedback} in the next node's prompt (e.g. keep_going). Empty until a check runs.
        self._last_feedback: str = ""

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
        # Stamp the latest usage record with the channel (purpose). Done
        # outside the exp_logger guard so token-bookkeeping works even
        # when no experiment logger is attached.
        latest = self._get_latest_usage()
        if latest is not None:
            latest.channel = purpose
        if self.exp_logger:
            self.exp_logger.log_llm_call(
                label, purpose, logged_prompt, response,
                latest,
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
            if "{feedback}" in prompt_text:
                fb = self._last_feedback or "(no judge feedback yet)"
                prompt_text = prompt_text.replace("{feedback}", fb)

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
                    correct, detail = self.ground_truth(self._problem, sc_last)
                    branch_entry["answer_correct"] = correct
                    branch_entry["answer_feedback"] = detail
                    # Surface the judge verdict to the next main-channel turn (keep_going).
                    self._last_feedback = detail
                    logger.info(
                        f"Answer check after {current_id} [visit {visit_num}]: "
                        f"{'CORRECT' if correct else 'INCORRECT'} "
                        f"(extracted: {sc_last!r})"
                    )
                    last_response = sc_last
                    if correct:
                        # Only flag as "terminated early" if we're actually
                        # bypassing unexhausted outgoing work. For graphs
                        # with no outgoing edge from this node (e.g. the
                        # single-node baseline), the loop would have ended
                        # naturally anyway — so "early termination" is a
                        # misnomer there.
                        has_remaining_work = False
                        for edge in graph.edges:
                            if edge.from_id != current_id:
                                continue
                            key = (edge.from_id, edge.to_id)
                            if key in edge_remaining and edge_remaining[key] <= 0:
                                continue  # this edge already exhausted
                            has_remaining_work = True
                            break

                        if has_remaining_work:
                            logger.info(
                                f"Early termination: correct answer after "
                                f"{current_id} [visit {visit_num}]"
                            )
                            self.terminated_early = True
                        else:
                            logger.info(
                                f"Correct answer after {current_id} "
                                f"[visit {visit_num}] (no remaining work to "
                                f"skip — not flagging as early termination)"
                            )
                        trace_entry["branches"].append(branch_entry)
                        own_trace.append(trace_entry)
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
        latest = self._get_latest_usage()
        if latest is not None:
            latest.channel = "main_channel"
        if self.exp_logger:
            self.exp_logger.log_llm_call("turn1", "solve", prompt, response, latest, reasoning=getattr(response, "reasoning", ""))
        return response


# Map of baseline names to classes for use by run_experiment / eval_task
BASELINE_AGENTS = {
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

    if config.agent_class in BASELINE_AGENTS:
        cls = BASELINE_AGENTS[config.agent_class]
        logger.info(f"Using baseline agent: {config.agent_class}")
        return cls(config, llm, ground_truth, logger=exp_logger)

    logger.warning(
        f"Unknown agent_class {config.agent_class!r}, "
        f"falling back to GoalTreeAgent"
    )
