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

import csv
import logging
import re
import traceback

# Codeforces target blobs (held-out tests + special-judge checker) can exceed the stdlib
# csv default field-size limit (128KB); raise it before any csv_dataset read.
csv.field_size_limit(10**8)
from datetime import datetime
from pathlib import Path
from typing import Any

from inspect_ai import Task, task
from inspect_ai.dataset import csv_dataset, Sample
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatCompletionChoice,
    ChatMessageUser,
    ModelOutput,
    get_model,
)
from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Target,
    accuracy,
    model_graded_qa,
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
# Bongard-specific ground-truth checker (for early termination)
# ---------------------------------------------------------------------------

def _make_bongard_ground_truth(
    target_rule: str,
    positives_json: str,
    negatives_json: str,
    probes_json: str = "",
):
    """Build a ground-truth checker for Bongard problems that inspects the
    solver's extracted Python function.

    The agent calls this after each side-channel invocation. The `solution`
    passed in is the final side-channel response — i.e. the `rule(s: str)
    -> bool` Python function that the solver emitted at `write_python_function`.
    The checker returns True iff the function correctly classifies ALL of:
      - the 6 positives  (rule(p) is True)
      - the 6 negatives  (rule(n) is False)
      - the held-out probe items   (rule(probe) == probe.label)

    Including probes in the early-termination bar raises it to match the
    python_rule_scorer's definition of "correct": overfitting the 12 shown
    items alone no longer short-circuits the agent's loop. The agent keeps
    iterating until the rule also generalizes to the probes.
    """
    import json as _json

    try:
        positives = _json.loads(positives_json) if positives_json else []
        negatives = _json.loads(negatives_json) if negatives_json else []
        probes_raw = _json.loads(probes_json) if probes_json else []
    except _json.JSONDecodeError:
        positives, negatives, probes_raw = [], [], []

    probes = [(p["item"], p.get("label") == "A") for p in probes_raw]

    def checker(problem: str, solution: str) -> tuple[bool, str]:
        # Fall back to never-matching if we don't have shown examples
        if not positives or not negatives:
            return False, "bongard checker: no positives/negatives available"

        code = _extract_python_code(solution)
        if code is None:
            return False, "bongard checker: no Python function in solution"

        namespace: dict = {}
        try:
            exec(compile(code, "<early_term_rule>", "exec"), namespace)
        except Exception as e:
            return False, f"bongard checker: exec error {type(e).__name__}: {e}"

        rule_fn = namespace.get("rule")
        if not callable(rule_fn):
            return False, "bongard checker: no callable `rule` defined"

        try:
            pos_ok = all(bool(rule_fn(p)) is True for p in positives)
            neg_ok = all(bool(rule_fn(n)) is False for n in negatives)
            probe_ok = all(bool(rule_fn(item)) == truth for item, truth in probes)
        except Exception as e:
            return False, f"bongard checker: rule raised {type(e).__name__}: {e}"

        shown_ok = pos_ok and neg_ok
        if not shown_ok:
            return False, "bongard checker: rule does not separate shown examples"
        if probes and not probe_ok:
            return False, (
                "bongard checker: rule fits shown examples but fails "
                f"{sum(1 for item, t in probes if bool(rule_fn(item)) != t)}/"
                f"{len(probes)} held-out probes (overfit)"
            )
        return True, (
            f"bongard checker: rule correctly classifies all "
            f"{len(positives)}+{len(negatives)} shown examples"
            + (f" and all {len(probes)} held-out probes"
               if probes else "")
        )

    return checker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _filter_side_channels_to_last_iteration(usage):
    """Drop side-channel records belonging to all but the last iteration.

    Each agent call gets stamped with a ``channel`` attribute on its
    LLMCallRecord — ``"main_channel"`` for main-graph nodes and
    ``"side:<id>"`` (possibly nested) for side channels. Records are appended
    in execution order, and side-channel calls always follow the main-channel
    node that triggered them. So the "last iteration" is everything from
    (and including) the last main-channel record onward, plus all earlier
    main-channel records. Side-channel records from earlier iterations are
    dropped.

    Records without a stamped channel (e.g. analysis/coding calls made
    outside an agent) are treated as main-channel-equivalent and kept.
    """
    last_main_idx = -1
    for i, r in enumerate(usage):
        ch = getattr(r, "channel", "") or ""
        if not ch.startswith("side:"):
            last_main_idx = i
    if last_main_idx < 0:
        return list(usage)
    kept = []
    for i, r in enumerate(usage):
        ch = getattr(r, "channel", "") or ""
        if not ch.startswith("side:"):
            kept.append(r)
        elif i > last_main_idx:
            kept.append(r)
    return kept


def _find_side_channel_node_response(execution_tree, node_id: str) -> str | None:
    """Walk an execution_tree backwards and return the response of the most
    recent side-channel trace node whose id matches `node_id`.

    The tree has the shape:
        [
          {node_id, prompt, response, branches: [
             {side_channel_id, trace: [{node_id, prompt, response}, ...],
              extracted_answer, ...}, ...
          ]},
          ...
        ]

    Returns the text of the matching node's response, or None if no branch
    contained a node with that id.
    """
    if not execution_tree:
        return None
    for entry in reversed(execution_tree):
        branches = entry.get("branches", []) if isinstance(entry, dict) else []
        for branch in reversed(branches):
            for trace_node in branch.get("trace", []):
                if trace_node.get("node_id") == node_id:
                    resp = trace_node.get("response")
                    # The serialized response may be str or an object with
                    # .full_text — execution_tree already flattened it to str
                    # via getattr(response, "full_text", response), so this
                    # is normally a plain string.
                    return resp if isinstance(resp, str) else str(resp)
    return None


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

    Bongard-text dataset extras (optional — passed through as metadata so
    the python-function scorer can evaluate extracted code against them):
        probes     — JSON list of {"item": str, "label": "A"|"B"} held-outs
        positives  — JSON list of Group-A example strings shown in the prompt
        negatives  — JSON list of Group-B example strings shown in the prompt
        answer_raw — raw mechanical description of the rule (for analysis)
        features   — JSON list of feature names the rule uses
    """
    metadata = {}
    if record.get("checker"):
        metadata["checker"] = record["checker"].strip()
    if record.get("valid_solutions"):
        metadata["valid_solutions"] = record["valid_solutions"].strip()
    if record.get("regex_pattern"):
        metadata["regex_pattern"] = record["regex_pattern"].strip()

    # Bongard pass-through fields (kept as raw JSON strings; scorer parses)
    for key in ("probes", "positives", "negatives", "answer_raw", "features"):
        if record.get(key):
            metadata[key] = record[key]

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
            # can stop early when it finds the correct answer.
            # For Bongard samples (which have positives/negatives/probes in
            # metadata) we swap in a Python-function-based checker: the
            # agent's early-termination hook fires when the solver's
            # extracted `rule(s)` function correctly classifies ALL of:
            #   - the 6 shown positives
            #   - the 6 shown negatives
            #   - the held-out probes
            # Including probes in the bar prevents overfit early-termination
            # (rule fits shown items but doesn't generalize).
            target = state.target.text if state.target else ""
            sample_meta = state.metadata or {}
            if sample_meta.get("positives") and sample_meta.get("negatives"):
                ground_truth = _make_bongard_ground_truth(
                    target_rule=target,
                    positives_json=sample_meta["positives"],
                    negatives_json=sample_meta["negatives"],
                    probes_json=sample_meta.get("probes", ""),
                )
            else:
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

        # Compute per-sample token usage. Drop side-channel records from
        # all but the last main-channel iteration: when an agent runs N
        # iterations each ending with a side-channel chain (e.g.
        # extract_rule → write_python_function), the earlier iterations'
        # side-channel tokens were repeating intermediate work and would
        # inflate the count if summed.
        sample_usage_raw = usage_log[usage_start:]
        sample_usage = _filter_side_channels_to_last_iteration(sample_usage_raw)
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

        # Pull the intermediate `extract_rule` response out of the side-channel
        # trace so the NL-judge scorer has something to judge. (The final
        # side-channel node — `write_python_function` — is what state.output
        # .completion holds.) Expects a side channel chain like:
        #     give_solution → extract_rule → write_python_function
        nl_rule = _find_side_channel_node_response(execution_tree, "extract_rule")
        if nl_rule is not None:
            state.metadata["nl_rule"] = nl_rule

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
# NL-judge scorer (LLM-as-judge on the natural-language rule description)
# ---------------------------------------------------------------------------

# Default grader: a fixed, strong model. Keeping it explicit here so it's
# always the same regardless of what solver model is under test.
DEFAULT_NL_JUDGE_MODEL = "anthropic/claude-opus-4-6"


def _build_feature_vocabulary_preamble() -> str:
    """Generate a concept-vocabulary preamble for the grader.

    Pulls feature names + one-line docstrings from the Bongard DSL so the
    grader sees the same canonical definitions the generator used. Without
    this, terms like "alpha sum" or "morse length" or "scrabble score" are
    ambiguous — the grader might interpret them differently than the
    ground-truth rule encodes.
    """
    # The bongard generator isn't on the main package path; add it so we
    # can import the DSL.
    import sys as _sys
    from pathlib import Path as _Path
    _gen_dir = str(_Path(__file__).parent / "generators" / "bongard")
    if _gen_dir not in _sys.path:
        _sys.path.insert(0, _gen_dir)
    try:
        from bongard_dsl import FEATURE_REGISTRY  # noqa
    except Exception:
        return ""  # degrade gracefully if import fails

    by_category: dict[str, list[str]] = {}
    for name, (fn, rtype, cat) in FEATURE_REGISTRY.items():
        doc = (fn.__doc__ or "").strip().split("\n")[0].strip()
        if not doc:
            continue
        line = f"  - `{name}` ({rtype.__name__}): {doc}"
        by_category.setdefault(cat, []).append(line)

    cat_order = [
        "Length/count", "Character identity", "Structure", "Alphabetic",
        "Digit", "Numeric", "Encoding", "Pattern", "Ratio",
    ]
    parts: list[str] = []
    for cat in cat_order:
        if cat not in by_category:
            continue
        parts.append(f"### {cat}")
        parts.extend(by_category[cat])
        parts.append("")
    # Any leftover categories
    for cat, lines in by_category.items():
        if cat in cat_order:
            continue
        parts.append(f"### {cat}")
        parts.extend(lines)
        parts.append("")

    return "\n".join(parts).rstrip()


NL_JUDGE_FEATURE_PREAMBLE = _build_feature_vocabulary_preamble()

# Custom template for Inspect AI's `model_graded_qa`.
# Substitutions used:
#   - {question} (inbuilt)  → state.input_text = the problem text (items shown)
#   - {criterion} (inbuilt) → target.text = the reference NL rule
#   - {nl_rule} (metadata)  → state.metadata["nl_rule"] = the solver's NL rule
#   - {instructions}        → NL_JUDGE_INSTRUCTIONS
#
# The feature-vocabulary preamble is pre-substituted at module load so it
# doesn't interfere with str.format() placeholder handling.
NL_JUDGE_TEMPLATE = ("""\
You are grading a Bongard-style puzzle. The solver was shown 6 positive \
and 6 negative string examples and asked to state the rule that \
distinguishes the two groups. Your job is to judge whether the candidate \
rule expresses the SAME concept as the reference rule.

---

## Concept vocabulary (features available to the generator)

These are the canonical definitions the ground-truth rule is built from.
Use them when checking equivalence (e.g. a candidate saying "the sum of the
letter values" refers to `alpha_sum` below).

""" + NL_JUDGE_FEATURE_PREAMBLE + """

---

## Grading criterion

The candidate is CORRECT iff it denotes the same classifier as the reference \
— i.e. for every possible item, both rules return the same True/False verdict.

The easiest way to check this in a concrete case is to apply both rules to \
the shown items (Group A and Group B below). If both rules say "Group A" \
on every A-item and "Group B" on every B-item, they agree on this sample. \
That's necessary but not quite sufficient for full equivalence; still, if \
the candidate agrees on all 12 shown items AND is phrased in terms of the \
same feature (see vocabulary) with the same decision boundary, it is \
equivalent.

Mark CORRECT when the candidate is:
  - logically or mathematically equivalent to the reference
    (e.g. "no odd digits" ≡ "all digits are even"; "each digit ≥ 3" ≡
     "the smallest digit is at least 3")
  - a paraphrase that partitions items identically

Mark INCORRECT when the candidate is:
  - strictly broader or narrower than the reference
    (e.g. "count of vowels is even" ≠ "count of vowels is zero")
  - about a different feature entirely
  - vague or non-committal (does not commit to a concrete decision rule)

---

## The puzzle instance

{question}

---

## Rules to compare

REFERENCE RULE — multiple equivalent forms of the ground truth:
  - plain:      {criterion}
  - mechanical: {answer_raw}
  - feature(s): {features}

CANDIDATE RULE (solver's answer):
  {nl_rule}

{instructions}
""")


NL_JUDGE_INSTRUCTIONS = """\
Think briefly about whether the candidate describes the same concept as the \
reference, applying the guidelines above. Then on the final line of your \
response, output exactly one of:

  GRADE: C
  GRADE: I
"""


@scorer(metrics=[accuracy(), stderr()])
def nl_rule_scorer(grader_model: str = DEFAULT_NL_JUDGE_MODEL,
                   verbose: bool = True):
    """LLM-as-judge scorer for Bongard natural-language rule descriptions.

    Delegates to Inspect AI's canonical `model_graded_qa`, but wraps the
    call in a locally-defined `score()` so there's an obvious place to put
    a `breakpoint()` or `print` when debugging. `{nl_rule}` in the template
    pulls from `state.metadata["nl_rule"]`.

    verbose=True emits one stderr line per call showing the grader's raw
    verdict — useful when a run is killed before the result JSON is
    written, because these go directly to the console.
    """
    import sys

    delegate = model_graded_qa(
        template=NL_JUDGE_TEMPLATE,
        instructions=NL_JUDGE_INSTRUCTIONS,
        model=grader_model,
    )

    async def score(state: TaskState, target: Target) -> Score:
        # ──────────────────────────────────────────────────────────────
        # PUT A BREAKPOINT HERE (or uncomment the next line) to confirm
        # this scorer is actually being invoked and to inspect `state`.
        # ──────────────────────────────────────────────────────────────
        # breakpoint()

        meta = state.metadata or {}
        nl_rule = (meta.get("nl_rule") or "").strip()

        if verbose:
            print(
                f"[nl_rule_scorer] sample={state.sample_id} "
                f"candidate={nl_rule[:120]!r} target={target.text[:120]!r}",
                file=sys.stderr, flush=True,
            )

        result = await delegate(state, target)

        # Inspect's model_graded_qa sets `Score.answer = state.output.completion`,
        # which for Bongard is the Python function (the final side-channel
        # output). That's misleading here — this scorer judged the NL rule,
        # not the code. Replace `answer` with the actual NL rule so the
        # scores JSON accurately records what was judged.
        if nl_rule:
            result = Score(
                value=result.value,
                answer=nl_rule,
                explanation=result.explanation,
                metadata=result.metadata,
            )

        if verbose:
            verdict_preview = (str(result.explanation) or "")[:400]
            print(
                f"[nl_rule_scorer] sample={state.sample_id} "
                f"→ {result.value} verdict={verdict_preview!r}",
                file=sys.stderr, flush=True,
            )

        return result

    return score


# ---------------------------------------------------------------------------
# Python-function scorer (executes the solver's extracted `rule(s)` function)
# ---------------------------------------------------------------------------

_PYTHON_CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


def _extract_python_code(completion: str) -> str | None:
    """Pull the first ```python ... ``` block out of a completion.

    If no fenced block is found, fall back to checking whether the whole
    completion itself looks like Python (starts with `def`).
    """
    if not completion:
        return None
    m = _PYTHON_CODE_BLOCK_RE.search(completion)
    if m:
        return m.group(1).strip()
    stripped = completion.strip()
    if stripped.startswith("def "):
        return stripped
    return None


def _evaluate_rule_on_items(
    rule_fn,
    items: list[str],
    expected_truth: bool,
) -> tuple[int, int, list[str]]:
    """Return (#correct, #errored, error_preview).

    Expected-truth semantics:
      - positives → rule should return True
      - negatives → rule should return False
    """
    correct = 0
    errored = 0
    errors: list[str] = []
    for s in items:
        try:
            pred = rule_fn(s)
        except Exception as e:
            errored += 1
            if len(errors) < 2:
                errors.append(f"{s!r}: {type(e).__name__}: {e}")
            continue
        if bool(pred) == expected_truth:
            correct += 1
    return correct, errored, errors


@scorer(metrics=[accuracy(), stderr()])
def python_rule_scorer(strict: bool = True):
    """Score by executing the solver's extracted `rule(s: str) -> bool`.

    The solver is prompted at the final side-channel node to emit a Python
    function that encodes the rule. This scorer:
      1. extracts the code from `state.output.completion`,
      2. exec()s it and looks up `rule`,
      3. runs it against the shown positives/negatives and held-out probes
         from `state.metadata`,
      4. reports a binary pass/fail.

    If `strict=True`, ALL items (shown + probes) must classify correctly.
    If `strict=False`, accuracy ≥ 0.9 counts as correct.

    Per-set accuracies are recorded in the Score's explanation for analysis.
    """
    import json as _json

    async def score(state: TaskState, target: Target) -> Score:
        completion = (state.output.completion or "").strip()
        code = _extract_python_code(completion)

        meta = state.metadata or {}
        try:
            positives = _json.loads(meta.get("positives", "[]"))
            negatives = _json.loads(meta.get("negatives", "[]"))
            probes_raw = _json.loads(meta.get("probes", "[]"))
        except _json.JSONDecodeError:
            return Score(
                value=INCORRECT,
                answer=completion[:120],
                explanation="failed to parse problem metadata (positives/negatives/probes)",
            )

        probe_items = [p["item"] for p in probes_raw]
        probe_labels_true = [p["label"] == "A" for p in probes_raw]

        if code is None:
            return Score(
                value=INCORRECT,
                answer=completion[:120],
                explanation="no Python function found in completion",
            )

        # Exec the code in an isolated namespace
        namespace: dict = {}
        try:
            exec(compile(code, "<solver_rule>", "exec"), namespace)
        except Exception as e:
            return Score(
                value=INCORRECT,
                answer=code[:200],
                explanation=f"compile/exec error: {type(e).__name__}: {e}",
            )

        rule_fn = namespace.get("rule")
        if not callable(rule_fn):
            return Score(
                value=INCORRECT,
                answer=code[:200],
                explanation="no callable `rule` defined in extracted code",
            )

        # Evaluate on the three sets
        pos_c, pos_e, pos_err = _evaluate_rule_on_items(rule_fn, positives, True)
        neg_c, neg_e, neg_err = _evaluate_rule_on_items(rule_fn, negatives, False)
        probe_c = 0
        probe_e = 0
        probe_errs: list[str] = []
        for item, truth in zip(probe_items, probe_labels_true):
            try:
                pred = rule_fn(item)
            except Exception as e:
                probe_e += 1
                if len(probe_errs) < 2:
                    probe_errs.append(f"{item!r}: {type(e).__name__}: {e}")
                continue
            if bool(pred) == truth:
                probe_c += 1

        n_pos = len(positives)
        n_neg = len(negatives)
        n_probes = len(probe_items)
        total = n_pos + n_neg + n_probes
        correct_count = pos_c + neg_c + probe_c

        pos_acc = pos_c / n_pos if n_pos else 0.0
        neg_acc = neg_c / n_neg if n_neg else 0.0
        probe_acc = probe_c / n_probes if n_probes else 0.0
        overall_acc = correct_count / total if total else 0.0

        if strict:
            passed = correct_count == total
        else:
            passed = overall_acc >= 0.9

        errors_str = ""
        if pos_err or neg_err or probe_errs:
            all_errs = (pos_err + neg_err + probe_errs)[:3]
            errors_str = " | runtime errors: " + "; ".join(all_errs)

        explanation = (
            f"pos {pos_c}/{n_pos} "
            f"(err {pos_e}) | "
            f"neg {neg_c}/{n_neg} "
            f"(err {neg_e}) | "
            f"probe {probe_c}/{n_probes} "
            f"(err {probe_e}) | "
            f"overall {overall_acc:.2%}"
            f"{errors_str}"
        )

        return Score(
            value=CORRECT if passed else INCORRECT,
            answer=code[:400],
            explanation=explanation,
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
    max_tokens: int | None = None,
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
            max_tokens=max_tokens,
            dataset_path=dataset_path,
        ),
        scorer=problem_scorer(),
    )


@task
def bongard_eval(
    dataset_path: str = "datasets/bongard_text.csv",
    config_path: str = "configs/config_bongard_scientist.yaml",
    model_override: str = "",
    thinking: bool | str = False,
    provider_override: str = "",
    base_url: str = "",
    experiment_log_dir: str = "",
    timeout: float | None = None,
    max_tokens: int | None = None,
    grader_model: str = DEFAULT_NL_JUDGE_MODEL,
    use_nl_judge: bool = True,
) -> Task:
    """Evaluate agents on Bongard-text problems.

    The solver's side-channel chain should end with a `write_python_function`
    node so `state.output.completion` is the extracted Python function.

    Two scorers run by default:
      - nl_rule_scorer     : LLM-as-judge (Opus 4.6) on the `extract_rule`
                             side-channel NL rule against the target rule.
      - python_rule_scorer : executes the extracted `rule(s)` function
                             against the 6+6 shown examples and probes.

    Pass `use_nl_judge=False` to skip the LLM judge and rely only on the
    deterministic code-based scorer (cheaper, no API cost, no grader
    misfires, but loses the NL-rule verdict).
    """
    # `python_rule_scorer` is listed FIRST so Inspect AI's display and the
    # top-level `correct`/`aggregate.accuracy` fields track the strict
    # code-based verdict (all 6+6+10 items classify correctly). The NL judge,
    # when enabled, runs concurrently as a secondary scorer.
    scorers: list = [python_rule_scorer(strict=True)]
    if use_nl_judge:
        scorers.append(nl_rule_scorer(grader_model=grader_model))

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
            max_tokens=max_tokens,
            dataset_path=dataset_path,
        ),
        scorer=scorers,
    )
