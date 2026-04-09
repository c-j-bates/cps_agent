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
    "step_back",
    "self_discover",
    "generate_vars",
]

MODEL_ORDER = [
    "claude-opus-4-6-instant",
    "claude-opus-4-6-thinking",
    "deepseek-chat",
    "deepseek-reasoner",
]

# Colors for each model (consistent across all plots).
MODEL_COLORS = {
    "claude-opus-4-6-instant": "#4C72B0",
    "claude-opus-4-6-thinking": "#012661",
    "deepseek-chat": "#DD8452",
    "deepseek-reasoner": "#C44E52",
}

STRATEGY_DISPLAY = {
    "baseline": "baseline",
    "keep_thinking_step_by_step": "keep-thinking-\nstep-by-step",
    "step_back": "step-back",
    "self_discover": "self-discover",
    "generate_vars": "generate-vars",
}

# Flat version (no newlines) for legend labels
STRATEGY_DISPLAY_FLAT = {
    "baseline": "baseline",
    "keep_thinking_step_by_step": "keep-thinking-step-by-step",
    "step_back": "step-back",
    "self_discover": "self-discover",
    "generate_vars": "generate-vars",
}

STRATEGY_COLORS = {
    "baseline": "#4C72B0",
    "keep_thinking_step_by_step": "#55A868",
    "step_back": "#C44E52",
    "self_discover": "#DD8452",
    "generate_vars": "#8172B3",
}


def load_results(results_dir: str) -> list[dict]:
    """Load all JSON result files from a directory."""
    results = []
    results_path = Path(results_dir)
    if not results_path.exists():
        print(f"Error: {results_dir} does not exist.")
        sys.exit(1)

    skipped = 0
    for f in sorted(results_path.glob("*.json")):
        with open(f) as fh:
            data = json.load(fh)
            data["_file"] = f.name
            # Skip empty/cancelled runs (no samples)
            if not data.get("samples"):
                skipped += 1
                print(f"  Skipping {f.name} (no samples — {data.get('eval_status', 'unknown')})")
                continue
            results.append(data)

    if not results:
        print(f"No result files with samples found in {results_dir}")
        sys.exit(1)

    return results


def canonical_strategy(config_name: str) -> str:
    """Extract the strategy name from a config_name string."""
    name = config_name.replace("config_minute_cryptic_", "").replace("config_", "")
    return name or "default"


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


def _mean_tokens_per_problem(r: dict, token_key: str = "total_tokens") -> float:
    """Compute mean tokens per problem (averaging across epochs per problem, then across problems)."""
    by_problem: dict[int, list[float]] = defaultdict(list)
    for s in r.get("samples", []):
        by_problem[s["id"]].append(s.get(token_key, 0))
    if not by_problem:
        return 0.0
    # Mean across epochs for each problem, then mean across problems
    return sum(sum(v) / len(v) for v in by_problem.values()) / len(by_problem)


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
            "correct": agg.get("num_correct", 0),
            "total": agg.get("num_samples", 0),
            "n_problems": n_problems,
            "mean_tokens": _mean_tokens_per_problem(r),
            "wall_time": agg.get("total_wall_time", 0),
            "llm_calls": agg.get("total_llm_calls", 0),
            "mean_steps": agg.get("mean_graph_steps", 0),
        })

    header = (
        f"{'Strategy':<30} {'Model':<25} {'Acc':>6} {'C/T':>7} "
        f"{'#Prob':>5} {'Mean Tok':>10} {'Time':>8} {'Calls':>6} {'Avg Steps':>10}"
    )
    sep = "-" * len(header)

    lines = [sep, header, sep]
    for row in rows:
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
        results, lambda r: _mean_tokens_per_problem(r)
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
        results, lambda r: _mean_tokens_per_problem(r)
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


def plot_tokens_vs_accuracy(results: list[dict], output_dir: Path):
    """Scatter plot of mean token usage vs accuracy, color-coded by model."""
    results = sort_results(results)

    fig, ax = plt.subplots(figsize=(8, 6))
    plotted_models = set()

    for r in results:
        model = canonical_model(r.get("model", "?"), r.get("thinking"))
        strategy = canonical_strategy(r.get("config_name", "?"))
        accuracy = r.get("aggregate", {}).get("accuracy", 0) * 100
        mean_tok = _mean_tokens_per_problem(r)
        color = MODEL_COLORS.get(model, "gray")

        label = model if model not in plotted_models else None
        plotted_models.add(model)

        ax.scatter(mean_tok, accuracy, color=color, label=label,
                   s=80, edgecolors="white", linewidth=0.5, zorder=3)
        # Annotate with strategy name
        display_strategy = STRATEGY_DISPLAY.get(strategy, strategy).replace("\n", " ")
        ax.annotate(display_strategy, (mean_tok, accuracy),
                    textcoords="offset points", xytext=(6, 4),
                    fontsize=7, alpha=0.7)

    ax.set_xlabel("Mean Tokens per Problem")
    ax.set_ylabel("Accuracy (%)")
    ax.set_title("Token Usage vs Accuracy")
    ax.legend(fontsize=8, loc="best")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
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
    n_cols = min(3, n_plots)
    n_rows = math.ceil(n_plots / n_cols)

    fig, axes = plt.subplots(n_rows, n_cols,
                             figsize=(4.5 * n_cols, 3.5 * n_rows),
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
    from tda_analysis.comparison import FEATURE_NAMES

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
                        help="TDA results directory (from tda_analysis.run_tda) "
                             "containing comparison/strategy_profiles.json")
    args = parser.parse_args()

    results = load_results(args.results_dir)
    print(f"\nLoaded {len(results)} result files from {args.results_dir}/\n")

    # Summary table
    table_str = print_summary_table(results)

    # Per-problem table
    problem_table = print_per_problem_table(results)

    # Overlap analysis
    overlap_table = ""
    disagree_table = ""
    if len(results) >= 2:
        overlap_table = print_pairwise_overlap(results, threshold=args.threshold)
        disagree_table = print_disagreement_table(results, threshold=args.threshold)

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
    print(f"\n  Saved: {output_dir / 'summary.txt'}")

    # LaTeX tables
    print("\nGenerating LaTeX tables...")
    generate_latex_summary(results, output_dir)
    generate_latex_per_problem(results, output_dir)

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
        plot_solution_iterations(results, output_dir)
        plot_iterations(results, output_dir)
        if len(results) >= 2:
            plot_upset(results, output_dir, threshold=args.threshold)
    else:
        print("\nSkipping plots (matplotlib not installed). pip install matplotlib")

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
