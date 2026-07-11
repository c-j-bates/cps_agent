"""
Analyze eval results and generate comparison tables and plots.

Reads JSON result files from eval_results/ and produces:
  - Summary table (accuracy, tokens, wall time, iterations)
  - Per-config accuracy comparison bar chart (grouped by strategy)
  - Token cost comparison bar chart (mean per problem, grouped by strategy)
  - Iteration count comparison (for multi-turn configs)
  - Scatter plot of token usage vs accuracy
  - Token usage for solved-only trials
  - Per-model, per-config histograms of solution iteration

Usage:
    python analyze_results.py
    python analyze_results.py --results-dir eval_results
    python analyze_results.py --output-dir analysis_output
"""

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

# Canonical orderings for consistent display across all plots and tables.
STRATEGY_ORDER = [
    "baseline",
    "keep_thinking_step_by_step",
    "keep_thinking_step_by_step_repeats10",
    "step_back",
    "self_discover",
    "generate_vars",
    "generate_vars_strict_chain",
]

MODEL_ORDER = [
    "claude-opus-4-6-instant",
    "claude-opus-4-6-thinking",
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-v4-pro-instant",
    "deepseek-v4-pro-thinking",
]

# Colors for each model (consistent across all plots). Each family uses a
# light/dark pair for the instant/thinking variants so they read as one model.
MODEL_COLORS = {
    "claude-opus-4-6-instant": "#4C72B0",   # mid blue
    "claude-opus-4-6-thinking": "#012661",  # navy
    "deepseek-chat": "#DD8452",             # warm orange
    "deepseek-reasoner": "#C44E52",         # rust
    "deepseek-v4-pro-instant": "#6BB392",   # soft sage
    "deepseek-v4-pro-thinking": "#1F6E47",  # deep forest
}

STRATEGY_DISPLAY = {
    "baseline": "baseline",
    "keep_thinking_step_by_step": "keep-thinking-\nstep-by-step",
    "keep_thinking_step_by_step_repeats10": "keep-thinking-\nstep-by-step\n(extended)",
    "step_back": "step-back",
    "self_discover": "self-discover",
    # "generate_vars": "generate-vars",
    "generate_vars": r"generate-$\Theta$",
    "generate_vars_strict_chain": "generate-$\\Theta$-\nstrict-chain",
}

# Flat version (no newlines) for legend labels
STRATEGY_DISPLAY_FLAT = {
    "baseline": "baseline",
    "keep_thinking_step_by_step": "keep-thinking-step-by-step",
    "keep_thinking_step_by_step_repeats10": "keep-thinking-step-by-step (extended)",
    "step_back": "step-back",
    "self_discover": "self-discover",
    # "generate_vars": "generate-vars",
    "generate_vars": r"generate-$\Theta$",
    "generate_vars_strict_chain": r"generate-$\Theta$-strict-chain",
}

STRATEGY_COLORS = {
    "baseline": "#4C72B0",
    "keep_thinking_step_by_step": "#55A868",
    "keep_thinking_step_by_step_repeats10": "#2D7041",  # deeper green — variant of keep-thinking
    "step_back": "#C44E52",
    "self_discover": "#DD8452",
    "generate_vars": "#8172B3",
    "generate_vars_strict_chain": "#17BECF",  # teal — visually distinct from baseline blue
}


_BONGARD_OVERALL_RE = re.compile(r"overall\s+([0-9.]+)%")


def _looks_like_code(s: str) -> bool:
    """Heuristic: does this string look like a Python code block?"""
    if not s:
        return False
    s = s.strip()
    return s.startswith("```") or s.startswith("def ")


def repair_bongard_json(data: dict) -> bool:
    """Fix the stale `scores.nl_rule_scorer.answer` field on each sample.

    Historically Inspect AI's `model_graded_qa` hardcoded Score.answer to
    state.output.completion, which for Bongard is the Python function.
    That left the NL-judge's recorded `answer` containing code instead of
    the NL rule. Samples have the real NL rule at the top-level
    `sample.nl_rule` field; copy it into `scores.nl_rule_scorer.answer`.

    Returns True if any sample was modified.
    """
    changed = False
    for s in data.get("samples") or []:
        scores = s.get("scores") or {}
        nl = scores.get("nl_rule_scorer")
        if not nl:
            continue
        if _looks_like_code(nl.get("answer") or "") and s.get("nl_rule"):
            nl["answer"] = s["nl_rule"]
            changed = True
    return changed


def _canonicalize_bongard_scores(data: dict) -> None:
    """If a result file has per-scorer Bongard data, rewrite each sample's
    top-level `correct`/`answer` fields (and the run's `aggregate`) to use
    the `python_rule_scorer` (code-based) verdict instead of whichever
    scorer happened to run first.

    Also extracts the per-sample item accuracy (fraction of 22 items the
    extracted Python function got right) from the scorer's explanation
    string and stores it on each sample + as `aggregate.mean_item_accuracy`.

    No-op for non-Bongard runs.
    """
    samples = data.get("samples") or []
    if not samples:
        return
    first_scores = samples[0].get("scores") or {}
    if "python_rule_scorer" not in first_scores:
        return  # not a Bongard run

    n_correct = 0
    n_total = 0
    item_accs = []
    for s in samples:
        scores = s.get("scores") or {}
        py = scores.get("python_rule_scorer")
        if py is None:
            continue
        s["correct"] = bool(py.get("correct"))
        s["answer"] = py.get("answer")
        m = _BONGARD_OVERALL_RE.search(str(py.get("explanation") or ""))
        if m:
            try:
                s["item_accuracy"] = float(m.group(1)) / 100.0
                item_accs.append(s["item_accuracy"])
            except ValueError:
                pass
        n_total += 1
        if s["correct"]:
            n_correct += 1

    agg = data.setdefault("aggregate", {})
    agg["num_samples"] = n_total
    agg["num_correct"] = n_correct
    agg["accuracy"] = (n_correct / n_total) if n_total else 0.0
    if item_accs:
        agg["mean_item_accuracy"] = sum(item_accs) / len(item_accs)


# `### Call N: <node_name> (<purpose>)`
# `**Tokens**: 1,234 in / 56 out | 1.2s | 3 tool rounds`
_MD_CALL_RE = re.compile(
    r"^### Call (?P<n>\d+): (?P<name>.+?) \((?P<purpose>[^)]+)\)\s*$",
    re.MULTILINE,
)
_MD_TOKENS_RE = re.compile(
    r"^\*\*Tokens\*\*:\s*"
    r"(?P<in>[\d,]+)\s*in\s*/\s*(?P<out>[\d,]+)\s*out"
    r"(?:\s*\|\s*(?P<wall>[\d.]+)s)?",
    re.MULTILINE,
)


def _parse_md_calls(md_path: Path) -> list[tuple[str, int, int, float]]:
    """Return [(purpose, in_tokens, out_tokens, wall_seconds), ...] in order.

    Reads the per-problem experiment-log markdown and pulls one record per
    `### Call …` heading. Returns an empty list if the file is unreadable
    or has no parseable calls.
    """
    try:
        text = md_path.read_text()
    except OSError:
        return []
    calls: list[tuple[str, int, int, float]] = []
    headers = list(_MD_CALL_RE.finditer(text))
    for i, m in enumerate(headers):
        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[start:end]
        tm = _MD_TOKENS_RE.search(block)
        if tm is None:
            continue
        in_tok = int(tm.group("in").replace(",", ""))
        out_tok = int(tm.group("out").replace(",", ""))
        wall = float(tm.group("wall") or 0.0)
        calls.append((m.group("purpose"), in_tok, out_tok, wall))
    return calls


def _filter_last_iter(calls: list[tuple[str, int, int, float]]
                      ) -> list[tuple[str, int, int, float]]:
    """Keep main-channel calls plus side-channel calls after the last main.

    Matches eval_task._filter_side_channels_to_last_iteration: when an
    agent runs N main iterations each followed by a side-channel chain,
    earlier iterations' side-channel tokens were repeated intermediate
    work and would inflate sums. Drop them; keep only the last
    iteration's side-channel chain.
    """
    is_side = [c[0].startswith("side:") for c in calls]
    last_main = -1
    for i, side in enumerate(is_side):
        if not side:
            last_main = i
    if last_main < 0:
        return list(calls)
    return [c for i, (c, side) in enumerate(zip(calls, is_side))
            if not side or i > last_main]


def _recompute_tokens_from_logs(data: dict, repo_root: Path) -> int:
    """Rewrite per-sample token counts on the loaded result dict in memory.

    Reads the per-problem ``.md`` logs under ``data['experiment_log_dir']``,
    applies ``_filter_last_iter``, and updates each sample's
    ``input_tokens``/``output_tokens``/``total_tokens``/
    ``wall_time_seconds``/``llm_calls`` with the filtered sums. Also
    refreshes the run-level ``aggregate`` token totals.

    The files on disk (both ``experiment_logs/`` and ``eval_results/``)
    are NOT modified — this is purely an in-memory adjustment so the
    plots and tables reflect the side-channel-filtered counts without
    touching your collected data.

    Returns the number of samples updated. Silently no-ops if the
    ``experiment_log_dir`` link is missing or no logs are parseable.
    """
    log_dir_str = data.get("experiment_log_dir") or ""
    if not log_dir_str:
        return 0
    log_dir = repo_root / log_dir_str
    if not log_dir.is_dir():
        return 0

    # {problem_id: [agg_for_epoch_in_chronological_order, ...]}
    per_problem: dict[int, list[dict]] = defaultdict(list)
    for subdir in sorted(log_dir.iterdir()):
        if not subdir.is_dir():
            continue
        md = subdir / f"{subdir.name}.md"
        if not md.is_file():
            continue
        # Get problem_id from sibling .json metadata, falling back to the
        # subdir-name suffix.
        meta_json = subdir / f"{subdir.name}.json"
        pid: int | None = None
        if meta_json.is_file():
            try:
                pid = int(json.loads(meta_json.read_text())
                          .get("metadata", {}).get("problem_id"))
            except (json.JSONDecodeError, TypeError, ValueError, OSError):
                pid = None
        if pid is None:
            m = re.search(r"_(\d+)$", subdir.name)
            if not m:
                continue
            pid = int(m.group(1))

        calls = _filter_last_iter(_parse_md_calls(md))
        in_sum = sum(c[1] for c in calls)
        out_sum = sum(c[2] for c in calls)
        wall_sum = sum(c[3] for c in calls)
        per_problem[pid].append({
            "input_tokens": in_sum,
            "output_tokens": out_sum,
            "total_tokens": in_sum + out_sum,
            "wall_time_seconds": round(wall_sum, 2),
            "llm_calls": len(calls),
        })

    if not per_problem:
        return 0

    samples = data.get("samples", [])
    seen_idx: dict[int, int] = defaultdict(int)
    updated = 0
    for s in samples:
        try:
            pid = int(s.get("id"))
        except (TypeError, ValueError):
            continue
        epochs = per_problem.get(pid)
        if not epochs:
            continue
        idx = seen_idx[pid]
        if idx >= len(epochs):
            continue
        agg = epochs[idx]
        seen_idx[pid] += 1
        for k, v in agg.items():
            s[k] = v
        updated += 1

    if updated:
        agg = data.setdefault("aggregate", {})
        agg["total_input_tokens"] = sum(s.get("input_tokens", 0) for s in samples)
        agg["total_output_tokens"] = sum(s.get("output_tokens", 0) for s in samples)
        agg["total_tokens"] = (
            agg["total_input_tokens"] + agg["total_output_tokens"]
        )
        if samples:
            agg["mean_tokens_per_sample"] = agg["total_tokens"] / len(samples)
        agg["total_wall_time"] = round(
            sum(s.get("wall_time_seconds", 0) for s in samples), 2
        )
        agg["total_llm_calls"] = sum(s.get("llm_calls", 0) for s in samples)

    return updated


def load_results(results_dir: str,
                 recompute_tokens: bool = True,
                 repo_root: Path | None = None) -> list[dict]:
    """Load all JSON result files from a directory.

    For Bongard runs (samples carry `scores.python_rule_scorer`), the
    top-level `correct`/`accuracy` fields are rewritten in-place to
    reflect the code-based scorer — so every downstream table/plot
    automatically reports code-scorer numbers rather than the LLM judge.

    When multiple runs share the same (canonical_strategy, canonical_model),
    the most recent one wins (filenames are timestamp-prefixed and sort
    lexically by recency). Superseded files stay on disk.

    When ``recompute_tokens=True`` (default), per-sample token counts are
    re-derived from the markdown experiment logs and the side-channel
    filter applied in memory. The files on disk are never modified.
    """
    results = []
    results_path = Path(results_dir)
    if not results_path.exists():
        print(f"Error: {results_dir} does not exist.")
        sys.exit(1)

    if repo_root is None:
        repo_root = Path(__file__).parent.resolve()

    skipped = 0
    n_token_updates = 0
    for f in sorted(results_path.glob("*.json")):
        with open(f) as fh:
            data = json.load(fh)
            data["_file"] = f.name
            # Skip empty/cancelled runs (no samples)
            if not data.get("samples"):
                skipped += 1
                print(f"  Skipping {f.name} (no samples — {data.get('eval_status', 'unknown')})")
                continue
            _canonicalize_bongard_scores(data)
            if recompute_tokens:
                n_token_updates += _recompute_tokens_from_logs(data, repo_root)
            results.append(data)

    if not results:
        print(f"No result files with samples found in {results_dir}")
        sys.exit(1)

    if n_token_updates:
        print(f"  Recomputed tokens (side-channel filter) for "
              f"{n_token_updates} samples from experiment_logs/")

    # Dedup by (strategy, model): keep the most recent run per key.
    # Filenames sort lexically by their YYYYMMDD_HHMMSS prefix, so the
    # last entry in a sorted group is the newest.
    by_key: dict[tuple[str, str], dict] = {}
    for r in sorted(results, key=lambda r: r.get("_file", "")):
        key = (
            canonical_strategy(r.get("config_name", "?")),
            canonical_model(r.get("model", "?"), r.get("thinking")),
        )
        by_key[key] = r
    if len(by_key) < len(results):
        superseded = len(results) - len(by_key)
        print(f"  Deduped {superseded} older run(s) by (strategy, model); "
              f"kept most-recent each")
    return list(by_key.values())


def canonical_strategy(config_name: str) -> str:
    """Extract the strategy name from a config_name string."""
    benchmark_strings = ["minute_cryptic_", "rosetta_", "bongard_", "generic_"]
    # name = config_name.replace("config_minute_cryptic_", "").replace("config_", "")
    for s in benchmark_strings:
        config_name = config_name.replace(s, "")
    config_name = config_name.replace("config_", "")
    return config_name or "default"


def canonical_model(model: str, thinking=None) -> str:
    """Map model + thinking flag to a canonical display name.

    Rules:
    - deepseek-reasoner with thinking=True  → "deepseek-reasoner"
    - deepseek-reasoner with thinking=False → "deepseek-chat"  (equivalent model)
    - deepseek-chat (any thinking value)    → "deepseek-chat"
    - claude-opus-4-6 with thinking truthy  → "claude-opus-4-6-thinking"
    - claude-opus-4-6 with thinking falsy   → "claude-opus-4-6-instant"
    """
    if model in ("deepseek-reasoner", "deepseek-chat"):
        if model == "deepseek-reasoner" and thinking:
            return "deepseek-reasoner"
        return "deepseek-chat"
    if model in ("deepseek-v4-pro",):
        if thinking and thinking is not False:
            return "deepseek-v4-pro-thinking"
        return "deepseek-v4-pro-instant"
    if model == "claude-opus-4-6":
        if thinking and thinking is not False:
            return "claude-opus-4-6-thinking"
        return "claude-opus-4-6-instant"
    # Fallback: keep original
    return model


# Keep old names as aliases for internal label-building that hasn't changed.
def short_config_name(config_name: str) -> str:
    return canonical_strategy(config_name)


def short_model_name(model: str, thinking=None) -> str:
    return canonical_model(model, thinking)


def _strategy_sort_key(strategy: str) -> int:
    try:
        return STRATEGY_ORDER.index(strategy)
    except ValueError:
        return len(STRATEGY_ORDER)


def _model_sort_key(model_name: str) -> int:
    try:
        return MODEL_ORDER.index(model_name)
    except ValueError:
        return len(MODEL_ORDER)


def sort_results(results: list[dict]) -> list[dict]:
    """Sort results by canonical strategy order, then model order."""
    return sorted(results, key=lambda r: (
        _strategy_sort_key(canonical_strategy(r.get("config_name", "?"))),
        _model_sort_key(canonical_model(r.get("model", "?"), r.get("thinking"))),
    ))


def _num_unique_problems(r: dict) -> int:
    """Count unique problem IDs in a result's samples."""
    return len(set(s["id"] for s in r.get("samples", [])))


def _mean_tokens_per_problem(r: dict, token_key: str = "output_tokens") -> float:
    """Compute mean tokens per problem (averaging across epochs per problem, then across problems)."""
    by_problem: dict[int, list[float]] = defaultdict(list)
    for s in r.get("samples", []):
        by_problem[s["id"]].append(s.get(token_key, 0))
    if not by_problem:
        return 0.0
    # Mean across epochs for each problem, then mean across problems
    return sum(sum(v) / len(v) for v in by_problem.values()) / len(by_problem)


def _median_tokens_per_problem(r: dict, token_key: str = "output_tokens") -> float:
    """Compute median tokens per problem (across epochs and problems)."""
    counts = []
    for s in r.get("samples", []):
        counts.append(s.get(token_key, 0))
    return np.median(counts)


def print_summary_table(results: list[dict]) -> str:
    """Print and return a formatted summary table."""
    results = sort_results(results)
    rows = []
    for r in results:
        agg = r.get("aggregate", {})
        n_problems = _num_unique_problems(r)
        rows.append({
            "strategy": canonical_strategy(r.get("config_name", "?")),
            "model": canonical_model(r.get("model", "?"), r.get("thinking")),
            "accuracy": agg.get("accuracy", 0),
            "item_accuracy": agg.get("mean_item_accuracy"),  # Bongard only
            "correct": agg.get("num_correct", 0),
            "total": agg.get("num_samples", 0),
            "n_problems": n_problems,
            "mean_tokens": _mean_tokens_per_problem(r),
            "median_tokens": _median_tokens_per_problem(r),
            "wall_time": agg.get("total_wall_time", 0),
            "llm_calls": agg.get("total_llm_calls", 0),
            "mean_steps": agg.get("mean_graph_steps", 0),
        })

    # Show the Item Acc column only if ANY run has it (= Bongard results)
    show_item_acc = any(row["item_accuracy"] is not None for row in rows)

    if show_item_acc:
        header = (
            f"{'Strategy':<30} {'Model':<25} {'Acc':>6} {'ItemAcc':>8} {'C/T':>7} "
            f"{'#Prob':>5} {'Mean Tok':>10} {'Time':>8} {'Calls':>6} {'Avg Steps':>10}"
        )
    else:
        header = (
            f"{'Strategy':<30} {'Model':<25} {'Acc':>6} {'C/T':>7} "
            f"{'#Prob':>5} {'Mean Tok':>10} {'Time':>8} {'Calls':>6} {'Avg Steps':>10}"
        )
    sep = "-" * len(header)

    lines = [sep, header, sep]
    for row in rows:
        item_cell = (f"{row['item_accuracy']:>7.1%}"
                     if row["item_accuracy"] is not None else f"{'—':>7}")
        if show_item_acc:
            line = (
                f"{row['strategy']:<30} {row['model']:<25} "
                f"{row['accuracy']:>5.1%} {item_cell:>8} "
                f"{row['correct']:>2}/{row['total']:<3} "
                f"{row['n_problems']:>5} {row['mean_tokens']:>10,.0f} "
                f"{row['wall_time']:>7.0f}s {row['llm_calls']:>6} "
                f"{row['mean_steps']:>10.1f}"
            )
        else:
            line = (
                f"{row['strategy']:<30} {row['model']:<25} "
                f"{row['accuracy']:>5.1%} {row['correct']:>2}/{row['total']:<3} "
                f"{row['n_problems']:>5} {row['mean_tokens']:>10,.0f} "
                f"{row['wall_time']:>7.0f}s {row['llm_calls']:>6} "
                f"{row['mean_steps']:>10.1f}"
            )
        lines.append(line)
    lines.append(sep)

    table_str = "\n".join(lines)
    print(table_str)
    return table_str


def print_per_problem_table(results: list[dict]) -> str:
    """Print per-problem accuracy matrix with epoch counts (e.g., 2/3)."""
    results = sort_results(results)
    all_problems = _all_problem_ids(results)
    run_labels, rates = _build_epoch_rates(results)

    has_epochs = any(
        r.get("epochs", 1) > 1 for r in results
    )

    # Column width — need room for "2/3" style marks when epochs > 1
    max_label = max(len(l) for l in run_labels) if run_labels else 10

    if has_epochs:
        title = "\nPer-Problem Results (correct/total epochs):"
    else:
        title = "\nPer-Problem Results (C=correct, X=incorrect):"
    header = f"{'Problem':<12} " + " ".join(f"{l:>{max_label}}" for l in run_labels)
    sep = "-" * len(header)
    lines = [title, sep, header, sep]

    for pid in all_problems:
        row = f"{str(pid):<12} "
        for label in run_labels:
            rate = rates[label].get(pid)
            if rate is None:
                mark = "-"
            elif has_epochs:
                mark = f"{rate[0]}/{rate[1]}"
            else:
                mark = "C" if rate[0] > 0 else "X"
            row += f"{mark:>{max_label}} "
        lines.append(row)

    lines.append(sep)
    table_str = "\n".join(lines)
    print(table_str)
    return table_str


def _latex_escape(s: str) -> str:
    """Escape special LaTeX characters."""
    for ch in ("&", "%", "$", "#", "_", "{", "}"):
        s = s.replace(ch, f"\\{ch}")
    return s


def generate_latex_summary(results: list[dict], output_dir: Path) -> str:
    """Generate a LaTeX table summarizing all runs."""
    results = sort_results(results)
    rows = []
    for r in results:
        agg = r.get("aggregate", {})
        rows.append({
            "strategy": canonical_strategy(r.get("config_name", "?")),
            "model": canonical_model(r.get("model", "?"), r.get("thinking")),
            "accuracy": agg.get("accuracy", 0),
            "correct": agg.get("num_correct", 0),
            "total": agg.get("num_samples", 0),
            "mean_tokens": _mean_tokens_per_problem(r),
            "median_tokens": _median_tokens_per_problem(r),
            "llm_calls": agg.get("total_llm_calls", 0),
            "mean_steps": agg.get("mean_graph_steps", 0),
        })

    lines = [
        r"\begin{table}[ht]",
        r"\centering",
        r"\caption{Evaluation results across prompting strategies and model sizes.}",
        r"\label{tab:eval-results}",
        r"\begin{tabular}{llrrrrr}",
        r"\toprule",
        r"Strategy & Model & Accuracy & Correct & Mean Tok/Prob & LLM Calls & Avg Steps \\",
        r"\midrule",
    ]

    prev_strategy = None
    for row in rows:
        strategy_display = _latex_escape(row["strategy"])
        if strategy_display == prev_strategy:
            strategy_display = ""
        else:
            if prev_strategy is not None:
                lines.append(r"\addlinespace")
            prev_strategy = strategy_display

        lines.append(
            f"  {strategy_display} & {_latex_escape(row['model'])} & "
            f"{row['accuracy']:.1%} & {row['correct']}/{row['total']} & "
            f"{row['mean_tokens']:,.0f} & {row['llm_calls']} & {row['mean_steps']:.1f} \\\\"
        )

    lines += [
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
    ]

    latex_str = "\n".join(lines)

    path = output_dir / "summary_table.tex"
    with open(path, "w") as f:
        f.write(latex_str + "\n")
    print(f"  Saved: {path}")

    return latex_str


def generate_latex_per_problem(results: list[dict], output_dir: Path) -> str:
    """Generate a LaTeX table with per-problem correctness matrix."""
    results = sort_results(results)
    all_problems = []
    seen = set()
    for r in results:
        for s in r.get("samples", []):
            pid = s["id"]
            if pid not in seen:
                all_problems.append(pid)
                seen.add(pid)

    run_labels = []
    for r in results:
        strategy = canonical_strategy(r["config_name"])
        model = canonical_model(r["model"], r.get("thinking"))
        run_labels.append(f"{strategy} {model}")

    n_runs = len(run_labels)
    col_spec = "l" + "c" * n_runs

    lines = [
        r"\begin{table}[ht]",
        r"\centering",
        r"\caption{Per-problem results (\cmark = correct, \xmark = incorrect).}",
        r"\label{tab:per-problem}",
        f"\\begin{{tabular}}{{{col_spec}}}",
        r"\toprule",
        "Problem & " + " & ".join(_latex_escape(l) for l in run_labels) + r" \\",
        r"\midrule",
    ]

    for pid in all_problems:
        cells = [str(pid)]
        for r in results:
            sample = next((s for s in r.get("samples", []) if s["id"] == pid), None)
            if sample:
                cells.append(r"\cmark" if sample["correct"] else r"\xmark")
            else:
                cells.append("--")
        lines.append(" & ".join(cells) + r" \\")

    lines.append(r"\midrule")
    total_cells = [r"\textbf{Total}"]
    for r in results:
        correct = sum(1 for s in r.get("samples", []) if s["correct"])
        total = len(r.get("samples", []))
        total_cells.append(f"\\textbf{{{correct}/{total}}}")
    lines.append(" & ".join(total_cells) + r" \\")

    lines += [
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
    ]

    latex_str = "\n".join(lines)

    path = output_dir / "per_problem_table.tex"
    with open(path, "w") as f:
        f.write(latex_str + "\n")
    print(f"  Saved: {path}")

    return latex_str


def _aggregate_epochs(samples: list[dict]) -> dict[int | str, dict]:
    """Aggregate multi-epoch samples into per-problem stats.

    Returns {problem_id: {"correct": count, "total": count, "samples": [...]}}
    """
    by_id: dict = defaultdict(lambda: {"correct": 0, "total": 0, "samples": []})
    for s in samples:
        pid = s["id"]
        by_id[pid]["total"] += 1
        if s.get("correct"):
            by_id[pid]["correct"] += 1
        by_id[pid]["samples"].append(s)
    return dict(by_id)


def _build_correctness_sets(
    results: list[dict], threshold: str = "any",
) -> tuple[list[str], dict[str, set]]:
    """Build {label: set_of_correct_problem_ids} for each run."""
    run_labels = []
    correctness: dict[str, set] = {}
    for r in results:
        label = f"{canonical_strategy(r['config_name'])}|{canonical_model(r['model'], r.get('thinking'))}"
        run_labels.append(label)
        agg = _aggregate_epochs(r.get("samples", []))
        correct_ids = set()
        for pid, stats in agg.items():
            if threshold == "any" and stats["correct"] >= 1:
                correct_ids.add(pid)
            elif threshold == "majority" and stats["correct"] > stats["total"] / 2:
                correct_ids.add(pid)
            elif threshold == "all" and stats["correct"] == stats["total"]:
                correct_ids.add(pid)
        correctness[label] = correct_ids
    return run_labels, correctness


def _build_epoch_rates(results: list[dict]) -> tuple[list[str], dict[str, dict]]:
    """Build {label: {problem_id: (correct_count, total_count)}} for each run."""
    run_labels = []
    rates: dict[str, dict] = {}
    for r in results:
        label = f"{canonical_strategy(r['config_name'])}|{canonical_model(r['model'], r.get('thinking'))}"
        run_labels.append(label)
        agg = _aggregate_epochs(r.get("samples", []))
        rates[label] = {pid: (s["correct"], s["total"]) for pid, s in agg.items()}
    return run_labels, rates


def _all_problem_ids(results: list[dict]) -> list:
    """Return unique problem IDs in stable order."""
    seen = set()
    ids = []
    for r in results:
        for s in r.get("samples", []):
            pid = s["id"]
            if pid not in seen:
                ids.append(pid)
                seen.add(pid)
    return ids


def print_pairwise_overlap(results: list[dict], threshold: str = "any") -> str:
    """Print pairwise overlap matrix: both correct, only A, only B, both wrong."""
    run_labels, sets = _build_correctness_sets(results, threshold=threshold)
    all_ids = set(_all_problem_ids(results))

    lines = ["\nPairwise Overlap (both_C / only_A / only_B / both_X):"]
    max_lbl = max(len(l) for l in run_labels)
    cell_w = 22
    header = f"{'':>{max_lbl}}  " + "  ".join(f"{l:>{cell_w}}" for l in run_labels)
    sep = "-" * len(header)
    lines += [sep, header, sep]

    for i, a in enumerate(run_labels):
        cells = []
        for j, b in enumerate(run_labels):
            if j <= i:
                cells.append(f"{'':>{cell_w}}")
                continue
            both_c = len(sets[a] & sets[b])
            only_a = len(sets[a] - sets[b])
            only_b = len(sets[b] - sets[a])
            both_x = len(all_ids - sets[a] - sets[b])
            cells.append(f"{both_c:>3}C {only_a:>3}A {only_b:>3}B {both_x:>3}X")
        row = f"{a:>{max_lbl}}  " + "  ".join(cells)
        lines.append(row)
    lines.append(sep)

    table_str = "\n".join(lines)
    print(table_str)
    return table_str


def print_disagreement_table(results: list[dict], threshold: str = "any") -> str:
    """Per-problem matrix sorted so disagreement rows appear first."""
    run_labels, sets = _build_correctness_sets(results, threshold=threshold)
    _, rates = _build_epoch_rates(results)
    all_ids = _all_problem_ids(results)
    has_epochs = any(r.get("epochs", 1) > 1 for r in results)

    rows: list[tuple[bool, list[str], list[str], object]] = []
    for pid in all_ids:
        pass_marks = []
        display_marks = []
        for label in run_labels:
            passed = pid in sets[label]
            pass_marks.append("C" if passed else "X")
            rate = rates[label].get(pid)
            if rate is None:
                display_marks.append("-")
            elif has_epochs:
                display_marks.append(f"{rate[0]}/{rate[1]}")
            else:
                display_marks.append("C" if passed else "X")
        has_disagree = len(set(pass_marks)) > 1
        rows.append((has_disagree, pass_marks, display_marks, pid))

    rows.sort(key=lambda r: (not r[0], str(r[3])))

    max_lbl = max(len(l) for l in run_labels)
    cell_w = max(max_lbl, 5)
    header = f"{'Problem':<12} " + " ".join(f"{l:>{cell_w}}" for l in run_labels) + "  Note"
    sep = "-" * len(header)
    threshold_note = f" (threshold: {threshold})" if has_epochs else ""
    lines = [f"\nPer-Problem Results (sorted by disagreement){threshold_note}:",
             sep, header, sep]

    for has_disagree, pass_marks, display_marks, pid in rows:
        row = f"{str(pid):<12} "
        row += " ".join(f"{m:>{cell_w}}" for m in display_marks)
        if has_disagree:
            solvers = [
                run_labels[i] for i, m in enumerate(pass_marks) if m == "C"
            ]
            all_solved = all(m == "C" for m in pass_marks)
            if not all_solved and solvers:
                row += f"  <-- only: {', '.join(solvers)}"
        lines.append(row)
    lines.append(sep)

    n_disagree = sum(1 for d, _, _, _ in rows if d)
    n_all_correct = sum(
        1 for _, pm, _, _ in rows if all(m == "C" for m in pm)
    )
    n_all_wrong = sum(
        1 for _, pm, _, _ in rows if all(m == "X" for m in pm)
    )
    lines.append(
        f"\n  {n_disagree} problems with disagreement, "
        f"{n_all_correct} solved by all, "
        f"{n_all_wrong} solved by none"
    )

    table_str = "\n".join(lines)
    print(table_str)
    return table_str


def plot_upset(results: list[dict], output_dir: Path, threshold: str = "any"):
    """UpSet-style bar chart showing sizes of all correctness intersections."""
    run_labels, sets = _build_correctness_sets(results, threshold=threshold)
    all_ids = _all_problem_ids(results)

    if len(run_labels) < 2:
        return

    from collections import Counter
    membership = Counter()
    for pid in all_ids:
        key = frozenset(l for l in run_labels if pid in sets[l])
        membership[key] += 1

    sorted_groups = sorted(membership.items(), key=lambda x: -x[1])

    n_groups = len(sorted_groups)
    n_runs = len(run_labels)

    fig, (ax_bar, ax_dot) = plt.subplots(
        2, 1,
        figsize=(max(8, n_groups * 0.8 + 2), 3 + n_runs * 0.4),
        gridspec_kw={"height_ratios": [3, n_runs], "hspace": 0.05},
        sharex=True,
    )

    x = list(range(n_groups))
    counts = [g[1] for g in sorted_groups]
    group_keys = [g[0] for g in sorted_groups]

    bars = ax_bar.bar(x, counts, color="#4C72B0", edgecolor="white")
    for bar_rect, cnt in zip(bars, counts):
        ax_bar.text(
            bar_rect.get_x() + bar_rect.get_width() / 2,
            bar_rect.get_height() + 0.3,
            str(cnt), ha="center", va="bottom", fontsize=9,
        )
    ax_bar.set_ylabel("# Problems")
    has_epochs = any(r.get("epochs", 1) > 1 for r in results)
    title = "Correctness Overlap (UpSet)"
    if has_epochs:
        title += f" [threshold: {threshold}]"
    ax_bar.set_title(title)
    ax_bar.set_xlim(-0.5, n_groups - 0.5)

    for row_idx, label in enumerate(run_labels):
        for col_idx, key in enumerate(group_keys):
            if label in key:
                ax_dot.plot(col_idx, row_idx, "o", color="#4C72B0", markersize=8)
            else:
                ax_dot.plot(col_idx, row_idx, "o", color="#DDDDDD", markersize=8)
    for col_idx, key in enumerate(group_keys):
        active = [i for i, l in enumerate(run_labels) if l in key]
        if len(active) >= 2:
            ax_dot.plot(
                [col_idx, col_idx], [min(active), max(active)],
                color="#4C72B0", linewidth=2,
            )

    ax_dot.set_yticks(range(n_runs))
    ax_dot.set_yticklabels(run_labels, fontsize=9)
    ax_dot.set_xlim(-0.5, n_groups - 0.5)
    ax_dot.set_ylim(-0.5, n_runs - 0.5)
    ax_dot.invert_yaxis()
    ax_dot.set_xticks([])
    ax_dot.grid(axis="y", linestyle=":", alpha=0.3)

    max_label_len = max(len(l) for l in run_labels) if run_labels else 10
    left_margin = min(0.45, 0.05 + max_label_len * 0.008)
    fig.subplots_adjust(hspace=0.05, left=left_margin)
    path = output_dir / "upset.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {path}")


# ---------------------------------------------------------------------------
# Grouped bar chart helpers
# ---------------------------------------------------------------------------

def _build_grouped_data(results: list[dict], value_fn):
    """Build data for grouped bar charts.

    Args:
        results: list of result dicts
        value_fn: callable(result_dict) -> numeric value for the bar

    Returns:
        (strategies, models_present, data) where
        strategies: list of strategy names in order
        models_present: list of model names in order
        data: dict[(strategy, model)] -> value (or None if missing)
    """
    results = sort_results(results)
    data: dict[tuple[str, str], float] = {}
    strategies_seen = set()
    models_seen = set()

    for r in results:
        strategy = canonical_strategy(r.get("config_name", "?"))
        model = canonical_model(r.get("model", "?"), r.get("thinking"))
        strategies_seen.add(strategy)
        models_seen.add(model)
        data[(strategy, model)] = value_fn(r)

    # Order by canonical ordering, keeping only what's present
    strategies = [s for s in STRATEGY_ORDER if s in strategies_seen]
    strategies += sorted(strategies_seen - set(STRATEGY_ORDER))
    models = [m for m in MODEL_ORDER if m in models_seen]
    models += sorted(models_seen - set(MODEL_ORDER))

    return strategies, models, data


def _draw_grouped_bars_generic(ax, groups, series, data, ylabel, title,
                               group_display, series_colors,
                               series_display=None,
                               value_fmt="{:.1f}", show_values=True):
    """Draw a grouped bar chart on the given axes.

    Args:
        groups: list of group keys (x-axis clusters)
        series: list of series keys (colored bars within each cluster)
        data: dict[(group, series)] -> value (or None if missing)
        group_display: dict mapping group key -> x-axis label
        series_colors: dict mapping series key -> color
        series_display: dict mapping series key -> legend label (default: identity)

    Only places bars for series that have data in each cluster,
    so clusters with fewer series stay compact and centered.
    """
    if series_display is None:
        series_display = {}
    n_groups = len(groups)
    if not series or n_groups == 0:
        return

    bar_width = 0.8 / len(series)
    x = np.arange(n_groups)
    legend_added = set()

    for gi, group in enumerate(groups):
        present = [s for s in series if data.get((group, s)) is not None]
        n_present = len(present)
        for j, s in enumerate(present):
            offset = (j - (n_present - 1) / 2) * bar_width
            color = series_colors.get(s, f"C{series.index(s)}")
            val = data[(group, s)]
            label = series_display.get(s, s) if s not in legend_added else None
            legend_added.add(s)
            bar = ax.bar(x[gi] + offset, val if val else 0, bar_width * 0.9,
                         label=label, color=color, edgecolor="white", linewidth=0.5)

            if show_values and val and val > 0:
                ax.text(bar[0].get_x() + bar[0].get_width() / 2,
                        bar[0].get_height(), value_fmt.format(val),
                        ha="center", va="bottom", fontsize=7)

    display_labels = [group_display.get(g, g) for g in groups]
    ax.set_xticks(x)
    ax.set_xticklabels(display_labels, fontsize=9, ha="center")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(fontsize=8, loc="best")


def _draw_grouped_bars(ax, strategies, models, data, ylabel, title,
                       value_fmt="{:.1f}", show_values=True):
    """Draw grouped bars with strategies on x-axis, models as colored bars."""
    _draw_grouped_bars_generic(
        ax, strategies, models, data, ylabel, title,
        group_display=STRATEGY_DISPLAY, series_colors=MODEL_COLORS,
        value_fmt=value_fmt, show_values=show_values,
    )


def _draw_grouped_bars_by_model(ax, strategies, models, data, ylabel, title,
                                value_fmt="{:.1f}", show_values=True):
    """Draw grouped bars with models on x-axis, strategies as colored bars."""
    transposed = {(m, s): v for (s, m), v in data.items()}
    _draw_grouped_bars_generic(
        ax, models, strategies, transposed, ylabel, title,
        group_display={m: m for m in models},
        series_colors=STRATEGY_COLORS,
        series_display=STRATEGY_DISPLAY_FLAT,
        value_fmt=value_fmt, show_values=show_values,
    )


def plot_accuracy(results: list[dict], output_dir: Path):
    """Grouped bar chart of accuracy by strategy, color-coded by model."""
    strategies, models, data = _build_grouped_data(
        results, lambda r: r.get("aggregate", {}).get("accuracy", 0) * 100
    )

    fig, ax = plt.subplots(figsize=(max(8, len(strategies) * 2.5), 5))
    _draw_grouped_bars(ax, strategies, models, data,
                       ylabel="Accuracy (%)",
                       title="Accuracy by Strategy + Model",
                       value_fmt="{:.1f}%")
    ax.set_ylim(0, 105)

    plt.tight_layout()
    path = output_dir / "accuracy.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {path}")


def plot_accuracy_by_model(results: list[dict], output_dir: Path):
    """Grouped bar chart of accuracy by model, color-coded by strategy."""
    strategies, models, data = _build_grouped_data(
        results, lambda r: r.get("aggregate", {}).get("accuracy", 0) * 100
    )

    fig, ax = plt.subplots(figsize=(max(8, len(models) * 2.5), 5))
    _draw_grouped_bars_by_model(ax, strategies, models, data,
                                ylabel="Accuracy (%)",
                                title="Accuracy by Model + Strategy",
                                value_fmt="{:.1f}%")
    ax.set_ylim(0, 105)

    plt.tight_layout()
    path = output_dir / "accuracy_by_model.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {path}")


def plot_tokens(results: list[dict], output_dir: Path):
    """Grouped bar chart of mean tokens per problem by strategy, color-coded by model."""
    strategies, models, data = _build_grouped_data(
        results,
        lambda r: _median_tokens_per_problem(r)
        # lambda r: _mean_tokens_per_problem(r)
    )

    fig, ax = plt.subplots(figsize=(max(8, len(strategies) * 2.5), 5))
    _draw_grouped_bars(ax, strategies, models, data,
                       ylabel="Mean Tokens per Problem",
                       title="Token Usage by Strategy + Model (mean per problem)",
                       value_fmt="{:,.0f}", show_values=True)

    plt.tight_layout()
    path = output_dir / "tokens.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {path}")


def plot_tokens_by_model(results: list[dict], output_dir: Path):
    """Grouped bar chart of mean tokens per problem by model, color-coded by strategy."""
    strategies, models, data = _build_grouped_data(
        results,
        lambda r: _median_tokens_per_problem(r)
        # lambda r: _mean_tokens_per_problem(r)
    )

    fig, ax = plt.subplots(figsize=(max(8, len(models) * 2.5), 5))
    _draw_grouped_bars_by_model(ax, strategies, models, data,
                                ylabel="Mean Tokens per Problem",
                                title="Token Usage by Model + Strategy (mean per problem)",
                                value_fmt="{:,.0f}", show_values=True)

    plt.tight_layout()
    path = output_dir / "tokens_by_model.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {path}")


def _tokens_per_problem_subset(r: dict, subset: str = "all",
                                stat: str = "median",
                                token_key: str = "output_tokens") -> float | None:
    """Per-problem token statistic, optionally restricted to (un)solved samples.

    Samples are grouped by problem ID; within each problem we average over
    epochs (matching ``_mean_tokens_per_problem``). The ``stat`` argument
    then aggregates across problems.

    subset:
        "all"      → every sample
        "solved"   → samples where s["correct"] is truthy
        "unsolved" → samples where s["correct"] is falsy

    Returns None when no samples match the subset (so callers can skip).
    """
    by_problem: dict[int, list[float]] = defaultdict(list)
    for s in r.get("samples", []):
        if subset == "solved" and not s.get("correct"):
            continue
        if subset == "unsolved" and s.get("correct"):
            continue
        by_problem[s["id"]].append(s.get(token_key, 0))
    if not by_problem:
        return None
    per_problem = [sum(v) / len(v) for v in by_problem.values()]
    if stat == "mean":
        return sum(per_problem) / len(per_problem)
    return float(np.median(per_problem))


def _subset_accuracy(r: dict, subset: str = "all") -> float | None:
    """Accuracy (%) over the ``subset`` of samples. Returns None if empty.

    For ``"solved"`` this is always 100%; for ``"unsolved"`` it's 0% — those
    aren't useful, but the function stays consistent with the token helper.
    """
    samples = r.get("samples", [])
    if subset == "all":
        if not samples:
            return None
        n_correct = sum(1 for s in samples if s.get("correct"))
        return 100.0 * n_correct / len(samples)
    matching = [
        s for s in samples
        if (subset == "solved" and s.get("correct"))
        or (subset == "unsolved" and not s.get("correct"))
    ]
    if not matching:
        return None
    n_correct = sum(1 for s in matching if s.get("correct"))
    return 100.0 * n_correct / len(matching)


def _draw_tokens_vs_accuracy_panel(ax, results: list[dict], *,
                                    subset: str, stat: str,
                                    show_legend: bool):
    """One scatter panel: tokens (stat over subset) vs accuracy."""
    try:
        from adjustText import adjust_text  # type: ignore
    except ImportError:
        adjust_text = None

    plotted_models = set()
    texts = []

    for r in results:
        model = canonical_model(r.get("model", "?"), r.get("thinking"))
        strategy = canonical_strategy(r.get("config_name", "?"))
        tok_stat = _tokens_per_problem_subset(r, subset=subset, stat=stat)
        if tok_stat is None:
            continue
        # Accuracy is always reported over "all" samples (so a point's y
        # value reflects the run's overall performance, even when the
        # x-axis is restricted to solved/unsolved samples — that's the
        # informative pairing).
        accuracy = r.get("aggregate", {}).get("accuracy", 0) * 100
        color = MODEL_COLORS.get(model, "gray")

        label = model if (show_legend and model not in plotted_models) else None
        plotted_models.add(model)

        ax.scatter(tok_stat, accuracy, color=color, label=label,
                   s=70, edgecolors="white", linewidth=0.5, zorder=3)
        display_strategy = STRATEGY_DISPLAY.get(strategy, strategy).replace("\n", " ")
        texts.append(ax.text(tok_stat, accuracy, display_strategy,
                             fontsize=6.5, alpha=0.85))

    if adjust_text is not None and texts:
        adjust_text(
            texts, ax=ax,
            arrowprops=dict(arrowstyle="-", color="gray", lw=0.4, alpha=0.5),
            expand=(1.6, 1.8),
            force_text=(0.8, 1.2),
            force_static=(0.4, 0.6),
            only_move={"text": "xy", "static": "xy"},
            min_arrow_len=8,
        )

    subset_lbl = {"all": "all", "solved": "solved", "unsolved": "unsolved"}[subset]
    stat_lbl = {"mean": "Mean", "median": "Median"}[stat]
    ax.set_xlabel(f"{stat_lbl} Output Tokens / Problem ({subset_lbl})")
    ax.set_ylabel("Overall Accuracy (%)")
    ax.set_title(f"{stat_lbl} tokens — {subset_lbl}")
    if show_legend:
        ax.legend(fontsize=7, loc="best")
    ax.grid(True, alpha=0.3)


def plot_tokens_vs_accuracy(results: list[dict], output_dir: Path):
    """Tokens-vs-accuracy scatter grid.

    Grid layout: rows = {median, mean}, columns = {all, solved, unsolved}.
    Each panel is a model-colored scatter of per-run (tokens, accuracy)
    pairs with strategy labels placed by ``adjustText`` (if installed).
    """
    results = sort_results(results)

    fig, axes = plt.subplots(2, 3, figsize=(18, 10), sharey=True)
    for row, stat in enumerate(["median", "mean"]):
        for col, subset in enumerate(["all", "solved", "unsolved"]):
            _draw_tokens_vs_accuracy_panel(
                axes[row][col], results,
                subset=subset, stat=stat,
                show_legend=(row == 0 and col == 0),
            )

    fig.suptitle("Output Tokens per Problem vs Accuracy "
                 "(rows: median / mean; cols: all / solved / unsolved)",
                 fontsize=12)
    plt.tight_layout(rect=(0, 0, 1, 0.96))
    path = output_dir / "tokens_vs_accuracy.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {path}")


def plot_tokens_solved(results: list[dict], output_dir: Path):
    """Grouped bar chart of mean tokens per problem for SOLVED trials only."""
    def _mean_tokens_solved(r):
        by_problem: dict[int, list[float]] = defaultdict(list)
        for s in r.get("samples", []):
            if s.get("correct"):
                by_problem[s["id"]].append(s.get("total_tokens", 0))
        if not by_problem:
            return None  # No solved problems
        return sum(sum(v) / len(v) for v in by_problem.values()) / len(by_problem)

    strategies, models, data = _build_grouped_data(results, _mean_tokens_solved)

    # Filter out entries where no problems were solved
    has_any = any(v is not None for v in data.values())
    if not has_any:
        print("  Skipping tokens_solved plot (no solved problems)")
        return

    fig, ax = plt.subplots(figsize=(max(8, len(strategies) * 2.5), 5))
    _draw_grouped_bars(ax, strategies, models, data,
                       ylabel="Mean Tokens per Problem (solved only)",
                       title="Token Usage for Solved Problems",
                       value_fmt="{:,.0f}", show_values=True)

    plt.tight_layout()
    path = output_dir / "tokens_solved.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {path}")


def plot_tokens_solved_by_model(results: list[dict], output_dir: Path):
    """Grouped bar chart of mean tokens for solved trials, grouped by model."""
    def _mean_tokens_solved(r):
        by_problem: dict[int, list[float]] = defaultdict(list)
        for s in r.get("samples", []):
            if s.get("correct"):
                by_problem[s["id"]].append(s.get("total_tokens", 0))
        if not by_problem:
            return None
        return sum(sum(v) / len(v) for v in by_problem.values()) / len(by_problem)

    strategies, models, data = _build_grouped_data(results, _mean_tokens_solved)
    has_any = any(v is not None for v in data.values())
    if not has_any:
        return

    fig, ax = plt.subplots(figsize=(max(8, len(models) * 2.5), 5))
    _draw_grouped_bars_by_model(ax, strategies, models, data,
                                ylabel="Mean Tokens per Problem (solved only)",
                                title="Token Usage for Solved Problems (by model)",
                                value_fmt="{:,.0f}", show_values=True)

    plt.tight_layout()
    path = output_dir / "tokens_solved_by_model.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {path}")


def _config_check_info(config_path: str) -> tuple[int, int] | None:
    """Return (preamble, max_checks) derived from a config YAML.

    *preamble*: number of main-graph steps before the first ``run_after``
    node (i.e. before the first answer check fires).

    *max_checks*: total number of answer-check opportunities.  This equals
    ``2 + max_repeats`` — one check at the first ``run_after`` node, one
    check when the repeating node is first reached via its incoming edge,
    then ``max_repeats`` further checks from the self-loop traversals
    (the runtime initialises a counter to ``max_repeats`` and decrements
    on each self-loop traversal).

    Returns *None* if the config cannot be loaded or has no side channels.
    """
    p = Path(config_path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent / config_path
    if not p.exists():
        return None
    with open(p) as f:
        cfg = yaml.safe_load(f)
    graph = cfg.get("graph", {})
    side_channels = graph.get("side_channels", [])
    if not side_channels:
        return None

    run_after_nodes: set[str] = set()
    for sc in side_channels:
        run_after_nodes.update(sc.get("run_after", []))
    if not run_after_nodes:
        return None

    # Build adjacency: node -> next node (first non-self-loop edge).
    edges = graph.get("edges", [])
    adj: dict[str, str] = {}
    max_repeats = 0
    for e in edges:
        src, dst = e["from"], e["to"]
        if src == dst:
            max_repeats = max(max_repeats, e.get("max_repeats", 0))
        elif src not in adj:
            adj[src] = dst

    # Walk from entry, counting steps (1-based graph_steps).
    node = graph.get("entry")
    step = 0
    visited = set()
    preamble = None
    while node and node not in visited:
        step += 1
        if preamble is None and node in run_after_nodes:
            preamble = step - 1
        visited.add(node)
        node = adj.get(node)

    if preamble is None:
        return None

    # max_checks = first run_after check (1) + first visit to repeating
    # node via incoming edge (1) + self-loop traversals (max_repeats).
    max_checks = 2 + max_repeats
    return preamble, max_checks


def _per_problem_output_tokens_solved(results: list[dict]) -> tuple[list, list[str], dict]:
    """Build per-problem output-token data for solved trials only.

    Returns:
        all_problems: problem IDs in stable insertion order across runs
        run_labels: "strategy|model" string per result
        data: {(run_label, problem_id) -> mean output_tokens across solved
              epochs, or None if no epoch of that run solved the problem}
    """
    results = sort_results(results)
    all_problems = _all_problem_ids(results)
    run_labels: list[str] = []
    data: dict[tuple[str, object], float | None] = {}

    for r in results:
        label = (
            f"{canonical_strategy(r['config_name'])}"
            f"|{canonical_model(r['model'], r.get('thinking'))}"
        )
        run_labels.append(label)
        by_problem: dict = defaultdict(list)
        for s in r.get("samples", []):
            if s.get("correct"):
                by_problem[s["id"]].append(s.get("output_tokens", 0))
        for pid in all_problems:
            vals = by_problem.get(pid)
            data[(label, pid)] = sum(vals) / len(vals) if vals else None

    return all_problems, run_labels, data


def print_per_problem_tokens_solved(results: list[dict]) -> str:
    """Per-problem output-token table for solved trials.

    Two-line header (strategy / model). Each column is sized to the wider
    of the strategy name, model name, or value width. Unsolved cells: '-'.
    """
    all_problems, run_labels, data = _per_problem_output_tokens_solved(results)
    if not run_labels:
        return ""

    parts = [tuple(l.split("|", 1)) for l in run_labels]
    value_w = 8
    col_widths = [max(value_w, len(s), len(m)) for s, m in parts]
    id_col_w = max(7, max(len(str(p)) for p in all_problems)) if all_problems else 7

    row_strat = f"{'':<{id_col_w}} " + " ".join(
        f"{s:>{w}}" for (s, _), w in zip(parts, col_widths)
    )
    row_model = f"{'Problem':<{id_col_w}} " + " ".join(
        f"{m:>{w}}" for (_, m), w in zip(parts, col_widths)
    )
    sep = "-" * len(row_model)

    lines = [
        "\nPer-Problem Output Tokens (solved trials only; '-' = not solved):",
        sep, row_strat, row_model, sep,
    ]

    for pid in all_problems:
        cells = []
        for label, w in zip(run_labels, col_widths):
            val = data.get((label, pid))
            cells.append(f"{val:>{w},.0f}" if val is not None else f"{'-':>{w}}")
        lines.append(f"{str(pid):<{id_col_w}} " + " ".join(cells))

    lines.append(sep)
    table_str = "\n".join(lines)
    print(table_str)
    return table_str


def _instant_runs_by_family(run_labels: list[str]) -> dict[str, list[str]]:
    """Group run labels by LLM family, keeping only non-thinking/non-reasoner variants.

    Thinking and reasoning variants emit tokens by construction (lots of
    inner monologue), so they make a poor signal of "tokens required by the
    problem itself" and are excluded from difficulty analyses.
    """
    by_family: dict[str, list[str]] = defaultdict(list)
    for label in run_labels:
        _, model = label.split("|", 1)
        if "thinking" in model or "reasoner" in model:
            continue
        by_family[_llm_family(model)].append(label)
    return dict(by_family)


def tokens_per_difficulty(results: list[dict], output_dir: Path) -> Path | None:
    """Group problems by # of family-strategies that solved them; report tokens.

    For each LLM family present (instant variants only):
      * k = number of strategies in that family that solved a given problem
      * mean_tokens_p = mean output tokens across those k solvers for problem p
      * Group problems by k, report median/mean of mean_tokens_p per group.

    Hypothesis being tested: harder problems (lower k) require more tokens —
    so median/mean tokens should rise as k decreases.

    Caveats:
      * k=0 is excluded (no solvers ⇒ no data, not "zero tokens").
      * Within-family analysis: difficulty signal and token measurement
        come from the same family, so there is residual circularity from
        shared training behavior.

    Returns the path to the generated plot (or None if no plottable data).
    """
    all_problems, run_labels, data = _per_problem_output_tokens_solved(results)
    if not run_labels:
        return None

    by_family = _instant_runs_by_family(run_labels)
    families = [f for f in ("opus", "deepseek") if f in by_family]
    families += sorted(set(by_family) - set(families))
    if not families:
        print("  Skipping tokens_per_difficulty (no instant-variant runs)")
        return None

    per_family_groups: dict[str, dict[int, list[float]]] = {}
    for family in families:
        labels = by_family[family]
        groups: dict[int, list[float]] = defaultdict(list)
        for pid in all_problems:
            tokens = [data[(l, pid)] for l in labels if data.get((l, pid)) is not None]
            k = len(tokens)
            if k == 0:
                continue
            groups[k].append(float(np.mean(tokens)))
        per_family_groups[family] = dict(groups)

    print("\nOutput Tokens vs Problem Difficulty (within LLM family; instant only)")
    for family in families:
        groups = per_family_groups[family]
        if not groups:
            continue
        n_strategies = len(by_family[family])
        print(f"\n  Family: {family}  ({n_strategies} strategies in scope)")
        print(f"    {'k':>3}  {'n_probs':>8}  {'median_tok':>11}  {'mean_tok':>11}")
        for k in sorted(groups):
            vals = groups[k]
            print(f"    {k:>3}  {len(vals):>8}  "
                  f"{np.median(vals):>11,.0f}  {np.mean(vals):>11,.0f}")

    if not HAS_MATPLOTLIB:
        return None
    plottable = [f for f in families if per_family_groups[f]]
    if not plottable:
        return None

    fig, axes = plt.subplots(1, len(plottable),
                             figsize=(5.5 * len(plottable), 4.5),
                             squeeze=False, sharey=False)
    axes = axes[0]
    for ax, family in zip(axes, plottable):
        groups = per_family_groups[family]
        ks = sorted(groups)
        medians = [float(np.median(groups[k])) for k in ks]
        means = [float(np.mean(groups[k])) for k in ks]
        ns = [len(groups[k]) for k in ks]
        ax.plot(ks, medians, "o-", label="median", color="#4C72B0", linewidth=2)
        ax.plot(ks, means, "s--", label="mean", color="#DD8452", linewidth=2)
        for k, m, n in zip(ks, medians, ns):
            ax.annotate(f"n={n}", (k, m), textcoords="offset points",
                        xytext=(5, 5), fontsize=7, alpha=0.7)
        ax.set_xlabel(f"k = # of {len(by_family[family])} family strategies that solved")
        ax.set_ylabel("Mean output tokens (across solvers per problem)")
        ax.set_title(f"{family} family")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        ax.set_xticks(ks)

    fig.suptitle(
        "Tokens required to solve, grouped by # of family strategies that solved\n"
        "Within-family analysis (instant variants only)",
        fontsize=10,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    path = output_dir / "tokens_per_difficulty.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {path}")
    return path


def plot_tokens_vs_difficulty_rank(results: list[dict], output_dir: Path) -> Path | None:
    """Per-strategy tokens vs problem difficulty rank.

    For each LLM family (instant variants only):
      * Define a problem's difficulty score = mean output tokens across all
        family strategies that solved it. Sort problems by this score
        (descending: hardest first).
      * Plot each strategy's per-problem tokens against this rank.

    Interpretation:
      * If all strategy curves rise together with the family-mean curve,
        the family agrees on which problems are hard.
      * If a strategy sits uniformly above others at every rank, it just
        uses more tokens generally (not difficulty-driven).
      * If a strategy's curve is flat while the family mean rises, that
        strategy ignores difficulty.

    Controls for the "stronger model uses more tokens overall" confounder
    by ranking via family-aggregate rather than any one strategy's tokens —
    but circularity is not fully broken (signal and measurement share a family).
    """
    all_problems, run_labels, data = _per_problem_output_tokens_solved(results)
    if not run_labels or not HAS_MATPLOTLIB:
        return None

    by_family = _instant_runs_by_family(run_labels)
    families = [f for f in ("opus", "deepseek") if f in by_family]
    families += sorted(set(by_family) - set(families))
    if not families:
        return None

    family_data: dict[str, dict] = {}
    for family in families:
        labels = by_family[family]
        scored = []
        for pid in all_problems:
            tokens = [data[(l, pid)] for l in labels if data.get((l, pid)) is not None]
            if not tokens:
                continue
            scored.append((pid, float(np.mean(tokens))))
        if not scored:
            continue
        scored.sort(key=lambda x: -x[1])  # hardest first
        family_data[family] = {
            "labels": labels,
            "problems_sorted": [s[0] for s in scored],
            "scores": [s[1] for s in scored],
        }

    if not family_data:
        return None

    n_subs = len(family_data)
    fig, axes = plt.subplots(1, n_subs, figsize=(6.5 * n_subs, 5),
                             squeeze=False, sharey=False)
    axes = axes[0]
    for ax, family in zip(axes, family_data):
        fd = family_data[family]
        problems_sorted = fd["problems_sorted"]
        xs = list(range(1, len(problems_sorted) + 1))
        for label in fd["labels"]:
            strategy, _ = label.split("|", 1)
            ys = []
            x_present = []
            for x_idx, pid in enumerate(problems_sorted, start=1):
                v = data.get((label, pid))
                if v is not None:
                    ys.append(v)
                    x_present.append(x_idx)
            color = STRATEGY_COLORS.get(strategy)
            ax.plot(x_present, ys, "o-",
                    label=STRATEGY_DISPLAY_FLAT.get(strategy, strategy),
                    color=color, markersize=4, alpha=0.75, linewidth=1.2)
        ax.plot(xs, fd["scores"], "k--",
                label="family mean (difficulty score)",
                alpha=0.6, linewidth=1.5)
        ax.set_xlabel("Problem rank (1 = hardest by family-mean tokens)")
        ax.set_ylabel("Output tokens")
        ax.set_title(f"{family} family ({len(fd['labels'])} strategies)")
        ax.legend(fontsize=7, loc="best")
        ax.grid(True, alpha=0.3)

    fig.suptitle(
        "Per-strategy tokens vs problem difficulty rank\n"
        "Difficulty score = mean tokens across family strategies (within-family)",
        fontsize=10,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    path = output_dir / "tokens_vs_difficulty_rank.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {path}")
    return path


def _per_problem_output_tokens_all(results: list[dict]):
    """Build per-problem output-token data over ALL non-errored attempts.

    Returns:
        all_problems, run_labels,
        tokens: {(label, pid) -> mean output_tokens across non-errored epochs}
        solved: {(label, pid) -> True if any epoch was correct}

    Errored samples (truthy ``error`` field — auth failures, timeouts, etc.)
    are excluded since they reflect infrastructure issues, not the strategy's
    own token-generation decisions. A (label, pid) pair is absent from the
    output dicts if every epoch errored.
    """
    results = sort_results(results)
    all_problems = _all_problem_ids(results)
    run_labels: list[str] = []
    tokens: dict[tuple[str, object], float] = {}
    solved: dict[tuple[str, object], bool] = {}

    for r in results:
        label = (
            f"{canonical_strategy(r['config_name'])}"
            f"|{canonical_model(r['model'], r.get('thinking'))}"
        )
        run_labels.append(label)
        by_pid: dict = defaultdict(list)
        any_correct: dict = defaultdict(bool)
        for s in r.get("samples", []):
            if s.get("error"):
                continue
            by_pid[s["id"]].append(s.get("output_tokens", 0))
            if s.get("correct"):
                any_correct[s["id"]] = True
        for pid in all_problems:
            vals = by_pid.get(pid)
            if vals:
                tokens[(label, pid)] = sum(vals) / len(vals)
                solved[(label, pid)] = any_correct[pid]

    return all_problems, run_labels, tokens, solved


def _family_expected_tokens(runs: list[str], all_problems: list,
                            tokens: dict, solved: dict) -> dict:
    """Per-problem mean output tokens across all family runs that solved it."""
    expected: dict = {}
    for pid in all_problems:
        vals = [tokens[(t, pid)] for t in runs if solved.get((t, pid), False)]
        if vals:
            expected[pid] = float(np.mean(vals))
    return expected


def _is_thinking_variant(label: str) -> bool:
    """True if the model in this label is a thinking/reasoning variant."""
    model = label.split("|", 1)[1]
    return "thinking" in model or "reasoner" in model


def plot_unsolved_tokens_vs_expected_by_strategy(results: list[dict],
                                                  output_dir: Path) -> list[Path]:
    """One subplot per strategy: actual vs. expected tokens, solved + failed.

    Same data as ``plot_unsolved_tokens_vs_expected`` but split out so each
    strategy gets its own panel — much less overplotting. One figure per LLM
    family; within each figure, columns = strategies, rows = (log-log, linear).
    Axes are shared within a figure for cross-strategy comparison.

    Thinking/reasoning variants are plotted alongside their instant peers in
    the same column (distinguished by a square marker), but they do NOT
    contribute to E(P) — E(P) is computed from instant variants only. This
    keeps the difficulty signal from being inflated by the longer outputs
    that reasoning models produce by construction.
    """
    all_problems, run_labels, tokens, solved = _per_problem_output_tokens_all(results)
    if not run_labels or not HAS_MATPLOTLIB:
        return []

    by_family_all: dict[str, list[str]] = defaultdict(list)
    for label in run_labels:
        _, model = label.split("|", 1)
        by_family_all[_llm_family(model)].append(label)

    by_family_instant = _instant_runs_by_family(run_labels)

    families = [f for f in ("opus", "deepseek") if f in by_family_all]
    families += sorted(set(by_family_all) - set(families))
    if not families:
        return []

    # Marker per variant; filled = failed, open = solved (handled below).
    VARIANT_MARKER = {"instant": "o", "thinking": "s"}

    written: list[Path] = []
    for family in families:
        instant_runs = by_family_instant.get(family, [])
        family_expected = _family_expected_tokens(
            instant_runs, all_problems, tokens, solved,
        )
        if not family_expected:
            continue

        # Group runs by (strategy, variant). Usually one label per cell.
        per_cell: dict[tuple[str, str], dict] = {}
        for s_label in by_family_all[family]:
            strategy, _ = s_label.split("|", 1)
            variant = "thinking" if _is_thinking_variant(s_label) else "instant"
            key = (strategy, variant)
            cell = per_cell.setdefault(key, {
                "solved": [], "unsolved": [],
                "n_solved_total": 0, "n_failed_total": 0,
            })
            for pid in all_problems:
                if (s_label, pid) not in tokens:
                    continue
                is_solved = solved.get((s_label, pid), False)
                if is_solved:
                    cell["n_solved_total"] += 1
                else:
                    cell["n_failed_total"] += 1
                if pid not in family_expected:
                    continue
                expected = family_expected[pid]
                actual = float(tokens[(s_label, pid)])
                (cell["solved"] if is_solved else cell["unsolved"]).append(
                    (expected, actual, pid),
                )

        strategies_in_family = sorted(
            {s for (s, _) in per_cell},
            key=_strategy_sort_key,
        )
        if not strategies_in_family:
            continue

        all_vals: list[float] = []
        for cell in per_cell.values():
            for pts in (cell["solved"], cell["unsolved"]):
                all_vals.extend(e for e, _, _ in pts)
                all_vals.extend(a for _, a, _ in pts)
        positive = [v for v in all_vals if v > 0]
        if not positive:
            continue
        lo_log = max(1.0, min(positive))
        hi = max(positive)

        n_strats = len(strategies_in_family)
        fig, axes = plt.subplots(
            2, n_strats,
            figsize=(3.6 * n_strats, 7.5),
            squeeze=False, sharex="row", sharey="row",
        )

        for col, strategy in enumerate(strategies_in_family):
            ax_log = axes[0][col]
            ax_lin = axes[1][col]
            # Fall back to a neutral grey if this strategy isn't in the palette
            # (otherwise the "solved" scatter, which uses facecolors="none" with
            # edgecolors=color, becomes invisible when color is None and the
            # legend entry is silently elided).
            color = STRATEGY_COLORS.get(strategy) or "#666666"
            disp = STRATEGY_DISPLAY_FLAT.get(strategy, strategy)

            # Per-variant exclusion counts (sum across variants overcounted
            # before when both instant and thinking ran on the same strategy,
            # e.g. baseline). Track separately and report each on its own line.
            n_excl_by_variant: dict[str, int] = {}

            for variant in ("instant", "thinking"):
                cell = per_cell.get((strategy, variant))
                if not cell:
                    continue
                unsolved = cell["unsolved"]
                solved_pts = cell["solved"]
                n_excl = cell["n_failed_total"] - len(unsolved)
                if n_excl > 0:
                    n_excl_by_variant[variant] = n_excl

                marker = VARIANT_MARKER[variant]
                prefix = "" if variant == "instant" else "thinking "

                for ax in (ax_log, ax_lin):
                    if unsolved:
                        ax.scatter(
                            [e for e, _, _ in unsolved],
                            [a for _, a, _ in unsolved],
                            color=color, alpha=0.7, s=32,
                            edgecolors="white", linewidth=0.4,
                            marker=marker,
                            label=f"{prefix}failed (n={len(unsolved)})",
                        )
                    if solved_pts:
                        ax.scatter(
                            [e for e, _, _ in solved_pts],
                            [a for _, a, _ in solved_pts],
                            facecolors="none", edgecolors=color,
                            alpha=0.7, s=32, linewidth=1.2,
                            marker=marker,
                            label=f"{prefix}solved (n={len(solved_pts)})",
                        )

            for ax in (ax_log, ax_lin):
                ax.plot([lo_log, hi], [lo_log, hi], "k--", alpha=0.4)
                ax.grid(True, alpha=0.3, which="both")
                ax.legend(fontsize=7, loc="lower right")

            if n_excl_by_variant:
                if len(n_excl_by_variant) == 1:
                    n = next(iter(n_excl_by_variant.values()))
                    excl_text = f"{n} excluded (solved by none)"
                else:
                    parts = [f"{v}: {n}" for v, n in n_excl_by_variant.items()]
                    excl_text = "excluded (solved by none)\n" + ", ".join(parts)
                ax_log.text(
                    0.02, 0.98,
                    excl_text,
                    transform=ax_log.transAxes, fontsize=6,
                    va="top", ha="left", alpha=0.65,
                )

            ax_log.set_xscale("log")
            ax_log.set_yscale("log")
            ax_log.set_xlim(lo_log * 0.7, hi * 1.3)
            ax_log.set_ylim(lo_log * 0.7, hi * 1.3)
            ax_lin.set_xlim(0, hi * 1.05)
            ax_lin.set_ylim(0, hi * 1.05)
            ax_log.set_title(disp, fontsize=9)
            ax_lin.set_xlabel("Expected tokens E(P)")
            if col == 0:
                ax_log.set_ylabel("Actual tokens (log)")
                ax_lin.set_ylabel("Actual tokens (linear)")

        fig.suptitle(
            f"{family} family — Actual vs. Expected tokens, per strategy\n"
            "filled = failed, open = solved; ● instant, ■ thinking "
            "(thinking excluded from E(P))",
            fontsize=9,
            y=0.995,
        )
        fig.tight_layout(rect=[0, 0, 1, 0.97])
        path = output_dir / f"unsolved_tokens_vs_expected_by_strategy_{family}.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        print(f"  Saved: {path}")
        written.append(path)

    return written


def plot_unsolved_tokens_vs_expected(results: list[dict], output_dir: Path) -> Path | None:
    """Counterfactual test of the 'reluctance to generate' null hypothesis.

    The null we want to refute: a strategy's apparent weakness is just early
    termination — on problems it failed, it gave up before generating the
    tokens a successful solver would have needed.

    Method (within LLM family, instant variants only):
      * For each (strategy S, problem P) where S did not solve P, compute
        E(P) = mean output_tokens of OTHER family strategies that DID solve
        P.
      * Compare S's actual output_tokens on its failed attempt(s) to E(P).

    Interpretation:
      * actual ≥ expected: S generated enough — failure isn't reluctance.
      * actual ≪ expected: consistent with reluctance / early termination.

    Output: per-strategy summary (n, median ratio, % ≥ expected) plus a
    log-log scatter of (E(P), actual) per family with diagonal reference.
    """
    all_problems, run_labels, tokens, solved = _per_problem_output_tokens_all(results)
    if not run_labels or not HAS_MATPLOTLIB:
        return None

    by_family = _instant_runs_by_family(run_labels)
    families = [f for f in ("opus", "deepseek") if f in by_family]
    families += sorted(set(by_family) - set(families))
    if not families:
        return None

    per_family: dict[str, dict[str, dict]] = {}
    summary_rows: list[dict] = []

    for family in families:
        runs = by_family[family]
        family_expected = _family_expected_tokens(runs, all_problems, tokens, solved)
        per_strategy: dict[str, dict] = {}
        for s_label in runs:
            solved_pts: list[tuple[float, float, object]] = []
            unsolved_pts: list[tuple[float, float, object]] = []
            for pid in all_problems:
                if (s_label, pid) not in tokens or pid not in family_expected:
                    continue
                expected = family_expected[pid]
                actual = float(tokens[(s_label, pid)])
                if solved.get((s_label, pid), False):
                    solved_pts.append((expected, actual, pid))
                else:
                    unsolved_pts.append((expected, actual, pid))
            if solved_pts or unsolved_pts:
                per_strategy[s_label] = {
                    "solved": solved_pts,
                    "unsolved": unsolved_pts,
                }
                if unsolved_pts:
                    ratios = [a / e for e, a, _ in unsolved_pts if e > 0]
                    if ratios:
                        summary_rows.append({
                            "family": family,
                            "strategy": s_label,
                            "n": len(unsolved_pts),
                            "median_ratio": float(np.median(ratios)),
                            "pct_above": 100.0 * sum(1 for r in ratios if r >= 1) / len(ratios),
                        })
        if per_strategy:
            per_family[family] = per_strategy

    if not per_family:
        print("  Skipping unsolved_tokens_vs_expected (no in-scope failures with reference)")
        return None

    print("\nUnsolved-Token Counterfactual (vs. family expected tokens)")
    print("  ≥ expected on failures ⇒ failure isn't simple reluctance to generate.")
    name_w = max((len(r["strategy"]) for r in summary_rows), default=10)
    print(f"  {'Strategy':<{name_w}}  {'n':>4}  {'med actual/expected':>20}  {'% ≥ expected':>14}")
    for row in sorted(summary_rows, key=lambda r: (r["family"], r["strategy"])):
        print(f"  {row['strategy']:<{name_w}}  {row['n']:>4}  "
              f"{row['median_ratio']:>20.2f}  {row['pct_above']:>13.0f}%")

    n_fams = len(per_family)
    fig, axes = plt.subplots(2, n_fams, figsize=(6.5 * n_fams, 10.5),
                             squeeze=False)
    for col, family in enumerate(per_family):
        per_strategy = per_family[family]
        ax_log = axes[0][col]
        ax_lin = axes[1][col]
        all_vals: list[float] = []
        for s_label, pts in per_strategy.items():
            strategy, _ = s_label.split("|", 1)
            label = STRATEGY_DISPLAY_FLAT.get(strategy, strategy)
            color = STRATEGY_COLORS.get(strategy)

            unsolved = pts["unsolved"]
            solved_pts = pts["solved"]
            all_vals.extend(e for e, _, _ in unsolved + solved_pts)
            all_vals.extend(a for _, a, _ in unsolved + solved_pts)

            for ax in (ax_log, ax_lin):
                if unsolved:
                    ax.scatter([e for e, _, _ in unsolved],
                               [a for _, a, _ in unsolved],
                               color=color, alpha=0.7, s=32,
                               edgecolors="white", linewidth=0.4,
                               label=f"{label} failed (n={len(unsolved)})")
                if solved_pts:
                    ax.scatter([e for e, _, _ in solved_pts],
                               [a for _, a, _ in solved_pts],
                               facecolors="none", edgecolors=color,
                               alpha=0.7, s=32, linewidth=1.2,
                               label=f"{label} solved (n={len(solved_pts)})")

        positive = [v for v in all_vals if v > 0]
        if positive:
            lo_log = max(1.0, min(positive))
            hi = max(positive)
            ax_log.plot([lo_log, hi], [lo_log, hi], "k--", alpha=0.5, label="y = x")
            ax_log.set_xlim(lo_log * 0.7, hi * 1.3)
            ax_log.set_ylim(lo_log * 0.7, hi * 1.3)
            ax_lin.plot([0, hi], [0, hi], "k--", alpha=0.5, label="y = x")
            ax_lin.set_xlim(0, hi * 1.05)
            ax_lin.set_ylim(0, hi * 1.05)

        ax_log.set_xscale("log")
        ax_log.set_yscale("log")
        ax_log.set_title(f"{family} family (log-log)")
        ax_lin.set_title(f"{family} family (linear)")
        for ax in (ax_log, ax_lin):
            ax.set_xlabel("Expected tokens E(P) (mean across family solvers)")
            ax.set_ylabel("Actual tokens on attempt")
            ax.legend(fontsize=6, loc="best", ncol=2)
            ax.grid(True, alpha=0.3, which="both")

    fig.suptitle(
        "Tokens Generated vs. Family-Expected Tokens (filled = failed, open = solved)\n"
        "Above y=x: generated more than expected; below: under expected budget",
        fontsize=10,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    path = output_dir / "unsolved_tokens_vs_expected.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {path}")
    return path


def save_per_problem_tokens_csv(results: list[dict], output_dir: Path) -> Path:
    """Save the full per-problem × per-run output-token matrix as CSV.

    Empty cells indicate the run did not solve that problem.
    """
    all_problems, run_labels, data = _per_problem_output_tokens_solved(results)
    path = output_dir / "per_problem_tokens.csv"
    with open(path, "w") as f:
        f.write("problem_id," + ",".join(run_labels) + "\n")
        for pid in all_problems:
            cells = [str(pid)]
            for label in run_labels:
                val = data.get((label, pid))
                cells.append(f"{val:.0f}" if val is not None else "")
            f.write(",".join(cells) + "\n")
    print(f"  Saved: {path}")
    return path


def _llm_family(model: str) -> str:
    """Group canonical model names into LLM families.

    Reasoning and instant variants of the same base LLM are commensurable
    enough to share a color scale; different LLM families are not.
    """
    if model.startswith("claude-opus"):
        return "opus"
    if model.startswith("deepseek"):
        return "deepseek"
    return model


def plot_per_problem_tokens(results: list[dict], output_dir: Path,
                            vmax_pct: float = 95.0):
    """Per-LLM-family heatmaps of output tokens per problem per (strategy, model).

    One subplot per LLM family (opus, deepseek, ...). Reasoning/instant
    variants of the same family share a subplot (and color scale) since
    they're commensurable; different families do not, since baseline token
    output differs systematically.

    Each subplot's color scale is independent and percentile-clipped at
    ``vmax_pct``. Solved cells are colored; unsolved cells are gray.
    """
    all_problems, run_labels, data = _per_problem_output_tokens_solved(results)
    if not run_labels or not all_problems:
        return

    by_family: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for label in run_labels:
        strategy, model = label.split("|", 1)
        by_family[_llm_family(model)].append((strategy, model, label))

    family_order = ["opus", "deepseek"]
    families = [f for f in family_order if f in by_family]
    families += sorted(set(by_family) - set(family_order))
    if not families:
        return

    n_problems = len(all_problems)
    n_fams = len(families)

    cmap = plt.cm.YlOrRd.copy()
    cmap.set_bad(color="#cccccc")

    cell_h = max(0.22, min(0.5, 12.0 / n_problems))
    sub_w_per_col = 1.1
    fig_h = max(6, n_problems * cell_h + 2.5)
    fig_w = max(8, sum(len(by_family[f]) for f in families) * sub_w_per_col
                + n_fams * 1.6 + 1.5)

    width_ratios = [max(1, len(by_family[f])) for f in families]
    fig, axes = plt.subplots(
        1, n_fams, figsize=(fig_w, fig_h),
        squeeze=False, sharey=True,
        gridspec_kw={"width_ratios": width_ratios},
    )
    axes = axes[0]

    for ax, family in zip(axes, families):
        ordered = sorted(
            by_family[family],
            key=lambda smc: (_strategy_sort_key(smc[0]), _model_sort_key(smc[1])),
        )
        labels = [l for _, _, l in ordered]
        tick_labels = [
            f"{STRATEGY_DISPLAY_FLAT.get(s, s)}\n{m}"
            for s, m, _ in ordered
        ]

        n_runs = len(labels)
        matrix = np.full((n_problems, n_runs), np.nan)
        for j, label in enumerate(labels):
            for i, pid in enumerate(all_problems):
                val = data.get((label, pid))
                if val is not None:
                    matrix[i, j] = val

        solved_vals = matrix[~np.isnan(matrix)]
        if solved_vals.size == 0:
            ax.set_title(f"{family}\n(no solved problems)", fontsize=9)
            ax.set_xticks([])
            continue

        vmin = float(solved_vals.min())
        vmax = float(np.percentile(solved_vals, vmax_pct))
        actual_max = float(solved_vals.max())
        clipped = vmax < actual_max

        im = ax.imshow(matrix, aspect="auto", cmap=cmap,
                       vmin=vmin, vmax=vmax, interpolation="nearest")

        ax.set_xticks(range(n_runs))
        ax.set_xticklabels(tick_labels, rotation=45, ha="right", fontsize=7)
        title = family
        if clipped:
            title += f" — 95th pct={vmax:,.0f}; max={actual_max:,.0f}"
        else:
            title += f" — max={actual_max:,.0f}"
        ax.set_title(title, fontsize=9)

        plt.colorbar(im, ax=ax, shrink=0.6,
                     extend="max" if clipped else "neither")

    axes[0].set_yticks(range(n_problems))
    axes[0].set_yticklabels([str(p) for p in all_problems], fontsize=7)
    axes[0].set_ylabel("Problem ID")

    fig.suptitle(
        "Output Tokens per Problem by Strategy (solved only; gray = not solved)\n"
        "Per-LLM-family color scales — token counts are not comparable across families",
        fontsize=10,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.96])

    path = output_dir / "per_problem_tokens.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {path}")


def plot_solution_iterations(results: list[dict], output_dir: Path):
    """Line plots of cumulative proportion solved vs answer-check iteration.

    One subplot per prompting strategy (baseline excluded), with a colored
    line per model.  The iteration number counts only the graph nodes after
    which the side-channel answer check fires (as dictated by ``run_after``
    in the config YAML).  Preamble nodes that precede the first check are
    not counted.

    iteration = graph_steps - preamble
    where preamble is the number of main-graph steps before the first
    ``run_after`` node.  The x-axis is capped at ``1 + max_repeats`` (from
    the config), and the y-axis shows proportion of total samples.
    """
    results = sort_results(results)

    # Compute preamble + max_checks per config_path (property of the graph).
    config_cache: dict[str, tuple[int, int] | None] = {}

    # {strategy: {model: {"solved_at": Counter, "total": int, "max_checks": int}}}
    by_strategy: dict[str, dict[str, dict]] = defaultdict(dict)
    # Track max_checks per strategy (should be consistent across models).
    strategy_max_checks: dict[str, int] = {}

    for r in results:
        strategy = canonical_strategy(r.get("config_name", "?"))
        if strategy == "baseline":
            continue
        model = canonical_model(r.get("model", "?"), r.get("thinking"))
        samples = r.get("samples", [])
        if not samples:
            continue

        config_path = r.get("config_path", "")
        if config_path not in config_cache:
            config_cache[config_path] = _config_check_info(config_path)
        info = config_cache[config_path]
        if info is None:
            continue
        preamble, max_checks = info

        if strategy not in strategy_max_checks:
            strategy_max_checks[strategy] = max_checks
        else:
            strategy_max_checks[strategy] = max(strategy_max_checks[strategy],
                                                max_checks)

        solved_at: dict[int, int] = defaultdict(int)
        for s in samples:
            if s.get("correct"):
                check_iter = s["graph_steps"] - preamble
                # Clamp to max_checks (shouldn't exceed, but be safe)
                check_iter = min(check_iter, max_checks)
                solved_at[check_iter] += 1

        by_strategy[strategy][model] = {
            "solved_at": dict(solved_at),
            "total": len(samples),
        }

    if not by_strategy:
        print("  Skipping solution_iterations plot (no non-baseline data)")
        return

    strat_keys = sorted(by_strategy.keys(), key=_strategy_sort_key)

    n_plots = len(strat_keys)
    # Single-row layout — easier to compose page-width side-by-side in LaTeX.
    n_cols = n_plots
    n_rows = 1

    fig, axes = plt.subplots(n_rows, n_cols,
                             figsize=(3.2 * n_cols, 3.0 * n_rows),
                             squeeze=False)

    # Find global y-max for consistent ylim.
    global_y_max = 0.0
    for strategy in strat_keys:
        model_data = by_strategy[strategy]
        mc = strategy_max_checks[strategy]
        for model, info in model_data.items():
            running = 0
            for i in range(1, mc + 1):
                running += info["solved_at"].get(i, 0)
            prop = running / info["total"] if info["total"] else 0
            global_y_max = max(global_y_max, prop)
    y_lim = min(1.0, global_y_max + 0.05)

    for idx, strategy in enumerate(strat_keys):
        row, col = divmod(idx, n_cols)
        ax = axes[row][col]
        model_data = by_strategy[strategy]
        mc = strategy_max_checks[strategy]

        for model in MODEL_ORDER:
            if model not in model_data:
                continue
            info = model_data[model]
            iters = list(range(1, mc + 1))
            cumulative = []
            running = 0
            for i in iters:
                running += info["solved_at"].get(i, 0)
                cumulative.append(running / info["total"] if info["total"] else 0)

            color = MODEL_COLORS.get(model, "gray")
            ax.plot(iters, cumulative, marker="o", markersize=4,
                    color=color, label=model, linewidth=1.5)

        display_strategy = STRATEGY_DISPLAY_FLAT.get(strategy, strategy)
        ax.set_title(display_strategy, fontsize=10)
        ax.set_xlabel("Answer-Check Iteration")
        ax.set_ylabel("Proportion Solved")
        ax.set_xticks(range(1, mc + 1))
        ax.set_xlim(0.5, mc + 0.5)
        ax.set_ylim(0, y_lim)
        ax.legend(fontsize=7, loc="best")
        ax.grid(True, alpha=0.3)

    for idx in range(n_plots, n_rows * n_cols):
        row, col = divmod(idx, n_cols)
        axes[row][col].set_visible(False)

    fig.suptitle("Cumulative Proportion Solved by Answer-Check Iteration (baseline excluded)",
                 fontsize=11)
    fig.tight_layout()
    path = output_dir / "solution_iterations.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {path}")


def plot_iterations(results: list[dict], output_dir: Path):
    """Box plot of graph steps per sample, grouped by config+model."""
    results = sort_results(results)
    labels = []
    all_steps = []
    for r in results:
        strategy = canonical_strategy(r.get("config_name", "?"))
        model = canonical_model(r.get("model", "?"), r.get("thinking"))
        display_strategy = STRATEGY_DISPLAY.get(strategy, strategy).replace("\n", " ")
        labels.append(f"{display_strategy}\n{model}")
        steps = [s["graph_steps"] for s in r.get("samples", []) if s.get("graph_steps", 0) > 0]
        all_steps.append(steps)

    if not any(all_steps):
        return

    fig, ax = plt.subplots(figsize=(max(8, len(labels) * 1.5), 5))
    bp = ax.boxplot(all_steps, labels=labels, patch_artist=True)
    for i, (patch, r) in enumerate(zip(bp["boxes"], sort_results(results))):
        model = canonical_model(r.get("model", "?"), r.get("thinking"))
        patch.set_facecolor(MODEL_COLORS.get(model, f"C{i}"))
    ax.set_ylabel("Graph Steps (LLM calls per problem)")
    ax.set_title("Iteration Counts by Strategy + Model")
    ax.tick_params(axis="x", labelsize=8)

    plt.tight_layout()
    path = output_dir / "iterations.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {path}")


# ---------------------------------------------------------------------------
# TDA creativity analysis integration
# ---------------------------------------------------------------------------

def load_tda_profiles(tda_results_dir: str) -> dict:
    """Load TDA strategy profiles from a tda_results comparison directory.

    Returns {strategy: {feature: {"mean": float, "se": float}}}.
    """
    profiles_path = Path(tda_results_dir) / "comparison" / "strategy_profiles.json"
    if not profiles_path.is_file():
        print(f"  Warning: {profiles_path} not found")
        return {}
    return json.loads(profiles_path.read_text())


def print_tda_summary(profiles: dict) -> str:
    """Print a summary table of TDA creativity features by strategy."""
    from tda_analysis_minute_cryptic.comparison import FEATURE_NAMES

    strategies = sorted(profiles.keys())
    lines = ["\n── TDA Creativity Feature Profiles ──\n"]

    header = f"{'Feature':<25s}"
    for s in strategies:
        header += f"  {s:>20s}"
    lines.append(header)
    lines.append("─" * len(header))

    for feat in FEATURE_NAMES:
        row = f"{feat:<25s}"
        for s in strategies:
            prof = profiles[s].get(feat, {})
            mean = prof.get("mean", 0)
            se = prof.get("se", 0)
            row += f"  {mean:>8.3f} ± {se:<8.3f}"
        lines.append(row)

    table_str = "\n".join(lines)
    print(table_str)
    return table_str


def epoch_consistency_analysis(results: list[dict], output_dir: Path) -> str:
    """Per-epoch accuracy, within-strategy variability across epochs, and
    statistical tests of between-strategy differences.

    Designed for multi-epoch DeepSeek runs (skipped if no run has epochs>=2).
    Writes ``output_dir/epoch_analysis.txt`` and returns the printable string.
    Outputs:
      - per-epoch accuracy (epoch1%, epoch2%, epoch3%) per (strategy, model)
      - SD / range across epochs
      - Cochran's Q test on per-problem any-of-3 outcomes across the
        prompt-only strategies (global "are strategies equal?" test)
      - Paired bootstrap (10k reps) of per-problem mean accuracy
        for generate_vars minus each other strategy
      - GEE binomial logistic regression (cluster=problem) for the same
        pairwise comparison, when statsmodels is available
    Each block runs separately per (dataset, llm_family) cell.
    """
    from collections import defaultdict
    import numpy as np

    # Group runs by (llm_family, dataset_label).  Treat each results dir
    # itself as one cell (so the caller's --results-dir picks the dataset).
    multi_epoch = [r for r in results if (r.get("epochs") or 1) >= 2]
    if not multi_epoch:
        msg = "epoch_consistency_analysis: no runs with epochs>=2; skipping."
        print(msg)
        return msg

    # Build a (problem, epoch, strategy, model, correct) long-form list
    rows = []
    for r in multi_epoch:
        strategy = canonical_strategy(r.get("config_name", "?"))
        model = canonical_model(r.get("model", "?"), r.get("thinking"))
        by_pid = defaultdict(list)
        for s in r.get("samples", []):
            if s.get("error"):
                continue
            by_pid[s["id"]].append(bool(s.get("correct")))
        # Only keep problems with exactly the expected number of epochs
        n_eps = r.get("epochs") or 1
        for pid, eps in by_pid.items():
            if len(eps) != n_eps:
                continue
            for ei, c in enumerate(eps, start=1):
                rows.append((strategy, model, pid, ei, int(c)))
    if not rows:
        return "epoch_consistency_analysis: no usable rows."

    # --- per-epoch accuracy + SD / range ---------------------------------
    by_cell = defaultdict(list)  # (strategy, model) -> [(pid, ep, correct)]
    for s, m, p, e, c in rows:
        by_cell[(s, m)].append((p, e, c))

    out_lines = ["── Per-epoch accuracy (deepseek multi-epoch runs) ──", ""]
    out_lines.append(
        f"{'strategy':<32s} {'model':<22s} {'ep1':>6s} {'ep2':>6s} {'ep3':>6s} "
        f"{'mean':>6s} {'SD':>5s} {'range':>6s}"
    )
    per_epoch_summary = {}
    for (strat, model), entries in sorted(by_cell.items()):
        max_ep = max(e for _, e, _ in entries)
        accs = []
        for ep in range(1, max_ep + 1):
            sub = [c for _, e, c in entries if e == ep]
            accs.append(float(np.mean(sub)) if sub else float("nan"))
        sd = float(np.std(accs, ddof=1)) if len(accs) > 1 else 0.0
        rng = (max(accs) - min(accs)) if accs else 0.0
        mean = float(np.mean(accs))
        per_epoch_summary[(strat, model)] = (accs, mean, sd, rng)
        ep_str = " ".join(f"{a*100:>5.1f}%" for a in accs[:3])
        # pad to width 21 (3 fields × 7 chars)
        ep_str = ep_str.ljust(21)
        out_lines.append(
            f"{strat:<32s} {model:<22s} {ep_str} "
            f"{mean*100:>5.1f}% {sd*100:>4.1f}pp {rng*100:>5.1f}pp"
        )
    out_lines.append("")

    # --- Cochran's Q on per-problem any-of-3 across prompt-only strategies ---
    try:
        from scipy import stats
    except ImportError:
        out_lines.append("Cochran's Q skipped: scipy not installed.")
        stats = None
    if stats is not None:
        # Restrict to non-thinking model (the one with multiple strategies).
        # Find the most common model across multi-epoch rows.
        from collections import Counter
        model_counts = Counter(m for _, m, _, _, _ in rows)
        main_model = model_counts.most_common(1)[0][0]
        sub = [(s, p, e, c) for s, m, p, e, c in rows if m == main_model]
        # any-of-N per (strategy, problem)
        any_correct = defaultdict(int)
        all_pids = set()
        all_strats = set()
        for s, p, _, c in sub:
            all_pids.add(p)
            all_strats.add(s)
            if c:
                any_correct[(s, p)] = 1
        # Build matrix N_problems x k_strategies
        strat_list = sorted(all_strats)
        pid_list = sorted(all_pids)
        # Only keep problems present in all strategies (proper Cochran setup)
        complete = [
            p for p in pid_list
            if all((s, p) in any_correct or (s, p) not in any_correct
                   for s in strat_list)
        ]
        # Actually require every strategy to have run this problem
        seen = defaultdict(set)
        for s, p, _, _ in sub:
            seen[s].add(p)
        complete = sorted(set.intersection(*(seen[s] for s in strat_list)))
        if len(complete) >= 5 and len(strat_list) >= 2:
            import numpy as _np
            M = _np.array(
                [[any_correct.get((s, p), 0) for s in strat_list]
                 for p in complete],
                dtype=int,
            )
            n_prob = M.shape[0]
            k = M.shape[1]
            C = M.sum(axis=0)        # per-strategy
            R = M.sum(axis=1)        # per-problem
            T = int(C.sum())
            num = (k - 1) * (k * (C ** 2).sum() - T ** 2)
            den = k * T - (R ** 2).sum()
            if den != 0:
                Q = num / den
                p_q = float(stats.chi2.sf(Q, df=k - 1))
                out_lines.append(
                    f"Cochran's Q (model={main_model}): "
                    f"k={k} strategies, n={n_prob} problems, "
                    f"Q={Q:.2f}, df={k-1}, p={p_q:.3e}"
                )
            else:
                out_lines.append("Cochran's Q: degenerate (denominator 0).")
        out_lines.append("")

    # --- Paired bootstrap of per-problem mean accuracy vs generate_vars ---
    ref = "generate_vars"
    if any(s == ref for s, _, _, _, _ in rows):
        # Per (strategy, problem) mean correct on the main model
        rng_seed = 0
        n_boot = 10000
        from collections import Counter
        model_counts = Counter(m for _, m, _, _, _ in rows)
        main_model = model_counts.most_common(1)[0][0]
        per_sp = defaultdict(list)
        for s, m, p, _, c in rows:
            if m != main_model:
                continue
            per_sp[(s, p)].append(c)
        # build pivot
        strat_list = sorted({s for s, _ in per_sp})
        pid_list = sorted({p for _, p in per_sp})
        # Only problems present in all strategies
        seen = defaultdict(set)
        for (s, p), _ in per_sp.items():
            seen[s].add(p)
        common = sorted(set.intersection(*(seen[s] for s in strat_list))) \
            if strat_list else []
        if common and ref in strat_list:
            rng = np.random.default_rng(rng_seed)
            g = np.array([np.mean(per_sp[(ref, p)]) for p in common])
            n = len(common)
            out_lines.append(
                f"Paired bootstrap (model={main_model}): "
                f"per-problem mean acc difference, "
                f"{ref} − strategy; n={n} problems, {n_boot:,} reps"
            )
            out_lines.append(
                f"  {'strategy':<32s} {'Δ (pp)':>8s} {'95% CI (pp)':>20s} "
                f"{'p (two-sided)':>14s}"
            )
            # Pre-sample bootstrap indices once
            idx = rng.integers(0, n, size=(n_boot, n))
            for s in strat_list:
                if s == ref:
                    continue
                o = np.array([np.mean(per_sp[(s, p)]) for p in common])
                diff = g - o
                obs = float(diff.mean())
                boot_means = diff[idx].mean(axis=1)
                ci_lo, ci_hi = np.percentile(boot_means, [2.5, 97.5])
                p_one = (float((boot_means <= 0).mean()) if obs > 0
                         else float((boot_means >= 0).mean()))
                p_two = min(1.0, 2 * p_one)
                out_lines.append(
                    f"  {s:<32s} {obs*100:>+7.1f} "
                    f"[{ci_lo*100:>+5.1f}, {ci_hi*100:>+5.1f}]   "
                    f"{p_two:>10.4f}"
                )
            out_lines.append("")

    # --- GEE logistic regression (cluster=problem) ----------------------
    try:
        import statsmodels.api as sm  # noqa: F401
        from statsmodels.formula.api import gee
        from statsmodels.genmod.cov_struct import Exchangeable
        import pandas as _pd
    except ImportError:
        out_lines.append("GEE skipped: statsmodels not installed.")
    else:
        from collections import Counter
        model_counts = Counter(m for _, m, _, _, _ in rows)
        main_model = model_counts.most_common(1)[0][0]
        df = _pd.DataFrame(
            [(s, p, e, c) for s, m, p, e, c in rows if m == main_model],
            columns=["strategy", "problem", "epoch", "correct"],
        )
        # Reference = generate_vars
        ref = "generate_vars"
        if ref in df["strategy"].unique():
            df["strategy"] = _pd.Categorical(
                df["strategy"],
                categories=[ref] + sorted(
                    s for s in df["strategy"].unique() if s != ref
                ),
            )
            res = gee(
                "correct ~ C(strategy)", "problem", data=df,
                cov_struct=Exchangeable(),
                family=sm.families.Binomial(),
            ).fit()
            out_lines.append(
                f"GEE logistic regression (model={main_model}, "
                f"cluster=problem; reference={ref})"
            )
            out_lines.append(
                f"  {'contrast':<58s} {'log-OR':>8s} {'SE':>6s} "
                f"{'z':>6s} {'p':>10s} {'OR':>6s}"
            )
            for name, b, se, z, p in zip(
                res.params.index, res.params.values,
                res.bse.values, res.tvalues.values, res.pvalues.values
            ):
                if name == "Intercept":
                    continue
                OR = float(np.exp(b))
                out_lines.append(
                    f"  {name:<58s} {b:>+7.3f} {se:>6.3f} {z:>+6.2f} "
                    f"{p:>10.3e} {OR:>6.3f}"
                )
            out_lines.append("")

    txt = "\n".join(out_lines)
    print(txt)
    (output_dir / "epoch_analysis.txt").write_text(txt + "\n")
    print(f"  Saved: {output_dir / 'epoch_analysis.txt'}")
    return txt


def main():
    parser = argparse.ArgumentParser(description="Analyze eval results")
    parser.add_argument("--results-dir", default="eval_results",
                        help="Directory containing result JSON files")
    parser.add_argument("--output-dir", default="analysis_output",
                        help="Directory for output plots and tables")
    parser.add_argument("--threshold", default="any",
                        choices=["any", "majority", "all"],
                        help="Epoch threshold for correctness: "
                             "'any' (>=1 epoch correct, default), "
                             "'majority' (>50%%), "
                             "'all' (every epoch correct)")
    parser.add_argument("--tda-results-dir", default=None,
                        metavar="DIR",
                        help="TDA results directory (from tda_analysis_minute_cryptic"
                             ".run_tda or tda_analysis_rosetta.run_tda) "
                             "containing comparison/strategy_profiles.json")
    parser.add_argument("--repair-bongard-jsons", action="store_true",
                        help="Rewrite each Bongard JSON in-place so that "
                             "`scores.nl_rule_scorer.answer` contains the "
                             "actual NL rule (taken from the sample's "
                             "top-level `nl_rule` field), instead of the "
                             "Python code block that Inspect AI's "
                             "model_graded_qa originally put there.")
    args = parser.parse_args()

    results = load_results(args.results_dir)
    print(f"\nLoaded {len(results)} result files from {args.results_dir}/\n")

    if args.repair_bongard_jsons:
        results_path = Path(args.results_dir)
        repaired_count = 0
        for data in results:
            if repair_bongard_json(data):
                repaired_count += 1
                # Re-serialize, excluding the transient "_file" and any
                # runtime-only fields we added
                out = {k: v for k, v in data.items() if k != "_file"}
                (results_path / data["_file"]).write_text(
                    json.dumps(out, indent=2)
                )
        print(f"Repair: rewrote {repaired_count} JSON file(s) "
              f"to fix scores.nl_rule_scorer.answer")
        # Reload so downstream display reflects the repaired state on disk
        results = load_results(args.results_dir)

    # Summary table
    table_str = print_summary_table(results)

    # Per-problem table
    problem_table = print_per_problem_table(results)

    # Overlap analysis
    overlap_table = ""
    disagree_table = ""
    tokens_table = ""
    if len(results) >= 2:
        overlap_table = print_pairwise_overlap(results, threshold=args.threshold)
        disagree_table = print_disagreement_table(results, threshold=args.threshold)
        tokens_table = print_per_problem_tokens_solved(results)

    # Save tables to file
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(output_dir / "summary.txt", "w") as f:
        f.write(table_str + "\n")
        f.write(problem_table + "\n")
        if overlap_table:
            f.write(overlap_table + "\n")
        if disagree_table:
            f.write(disagree_table + "\n")
        if tokens_table:
            f.write(tokens_table + "\n")
    print(f"\n  Saved: {output_dir / 'summary.txt'}")

    # LaTeX tables
    print("\nGenerating LaTeX tables...")
    generate_latex_summary(results, output_dir)
    generate_latex_per_problem(results, output_dir)

    # CSV exports (matplotlib-independent)
    if len(results) >= 2:
        save_per_problem_tokens_csv(results, output_dir)

    # Plots
    if HAS_MATPLOTLIB:
        print("\nGenerating plots...")
        plot_accuracy(results, output_dir)
        plot_accuracy_by_model(results, output_dir)
        plot_tokens(results, output_dir)
        plot_tokens_by_model(results, output_dir)
        plot_tokens_vs_accuracy(results, output_dir)
        plot_tokens_solved(results, output_dir)
        plot_tokens_solved_by_model(results, output_dir)
        if len(results) >= 2:
            plot_per_problem_tokens(results, output_dir)
            tokens_per_difficulty(results, output_dir)
            plot_tokens_vs_difficulty_rank(results, output_dir)
            plot_unsolved_tokens_vs_expected(results, output_dir)
            plot_unsolved_tokens_vs_expected_by_strategy(results, output_dir)
        plot_solution_iterations(results, output_dir)
        plot_iterations(results, output_dir)
        if len(results) >= 2:
            plot_upset(results, output_dir, threshold=args.threshold)
    else:
        print("\nSkipping plots (matplotlib not installed). pip install matplotlib")

    # Multi-epoch consistency + statistical tests (auto-skips if no
    # run has epochs>=2 in the loaded results)
    epoch_consistency_analysis(results, output_dir)

    # TDA creativity analysis (optional)
    if args.tda_results_dir:
        profiles = load_tda_profiles(args.tda_results_dir)
        if profiles:
            tda_summary = print_tda_summary(profiles)
            with open(output_dir / "summary.txt", "a") as f:
                f.write("\n" + tda_summary + "\n")

    print("\nDone.")


if __name__ == "__main__":
    main()
