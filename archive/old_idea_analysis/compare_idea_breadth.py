"""
Compare Idea Generation Breadth: baseline vs alternative strategy.

Processes the first k problems (with optional epoch repeats) and counts
total ideas + per-category ideas for each strategy.  Artifacts are cached
in each experiment's idea_graph_analysis/ directory for reuse.

Usage:
    # All problems, 1 epoch, baseline vs generate_vars
    python compare_idea_breadth.py \\
        --eval-dir eval_results/minute-cryptic-par1 \\
        --model deepseek-chat --strategy generate_vars

    # First 10 problems, 3 epochs each
    python compare_idea_breadth.py \\
        --eval-dir eval_results/minute-cryptic-par1 \\
        --model deepseek-chat --strategy generate_vars \\
        --n-problems 10 --n-epochs 3

    # Dry run (show what would be analyzed)
    python compare_idea_breadth.py \\
        --eval-dir eval_results/minute-cryptic-par1 \\
        --model deepseek-chat --strategy generate_vars --dry-run

    # Print results from existing output
    python compare_idea_breadth.py --print-results comparison_output/.../results.json
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from analyze_results import canonical_model, canonical_strategy


# ─── Discovery (reuse from pilot) ───────────────────────────────────────────

def load_eval_results(eval_dir: str) -> list[dict]:
    results = []
    for f in sorted(Path(eval_dir).glob("*.json")):
        data = json.loads(f.read_text())
        data["_file"] = str(f)
        if data.get("samples"):
            results.append(data)
    return results


def discover_runs(eval_dir: str) -> dict:
    """Returns {effective_model: {strategy: result_dict}}."""
    results = load_eval_results(eval_dir)
    by_model = defaultdict(dict)
    for r in results:
        model = canonical_model(r["model"], r.get("thinking"))
        strategy = canonical_strategy(r["config_name"])
        if strategy not in by_model[model] or \
                len(r["samples"]) > len(by_model[model][strategy]["samples"]):
            by_model[model][strategy] = r
    return dict(by_model)


def get_problem_ids(result: dict) -> list[int]:
    """Get unique problem IDs in order of first appearance."""
    seen = set()
    ids = []
    for s in result["samples"]:
        pid = s["id"]
        if pid not in seen:
            ids.append(pid)
            seen.add(pid)
    return ids


# ─── Main ───────────────────────────────────────────────────────────────────

def run_comparison(
    eval_dir: str,
    model: str | None,
    strategy: str,
    n_problems: int | None,
    n_epochs: int,
    output_dir: str,
    dry_run: bool,
):
    discovery = discover_runs(eval_dir)

    # Select model
    available_models = list(discovery.keys())
    if model:
        if model not in discovery:
            print(f"Error: model '{model}' not found. Available: {available_models}")
            sys.exit(1)
        selected_model = model
    elif len(available_models) == 1:
        selected_model = available_models[0]
    else:
        print(f"Multiple models found: {available_models}")
        print("Use --model to select one.")
        sys.exit(1)

    runs = discovery[selected_model]
    if "baseline" not in runs:
        print("Error: no baseline run found")
        sys.exit(1)
    if strategy not in runs:
        available = sorted(s for s in runs if s != "baseline")
        print(f"Error: strategy '{strategy}' not found. Available: {available}")
        sys.exit(1)

    baseline_result = runs["baseline"]
    alt_result = runs[strategy]

    # Determine problem IDs — intersection, first k
    baseline_pids = get_problem_ids(baseline_result)
    alt_pids_set = {s["id"] for s in alt_result["samples"]}
    common_pids = [pid for pid in baseline_pids if pid in alt_pids_set]

    if n_problems is not None:
        common_pids = common_pids[:n_problems]

    print(f"Model: {selected_model}")
    print(f"Strategies: baseline vs {strategy}")
    print(f"Problems: {len(common_pids)} (n_epochs={n_epochs})")
    print(f"Problem IDs: {common_pids}")

    if dry_run:
        print("\n[Dry run — stopping before LLM calls]")
        return

    # Run analyses
    from idea_graph_analysis import (
        _load_template, _make_client, analyze_one,
        find_mds_for_problem, cache_dir_for,
    )

    analysis_template = _load_template("idea_graph_analysis_prompt.txt")
    dedup_template = _load_template("idea_graph_dedup_prompt.txt")
    client = _make_client()

    baseline_exp_dir = baseline_result.get("experiment_log_dir", "")
    alt_exp_dir = alt_result.get("experiment_log_dir", "")

    all_metrics = []  # list of {strategy, problem_id, epoch, ...metrics}

    for strat_name, exp_dir in [("baseline", baseline_exp_dir),
                                (strategy, alt_exp_dir)]:
        cache = cache_dir_for(exp_dir)

        print(f"\n{'─' * 60}")
        print(f"Strategy: {strat_name}  ({exp_dir})")
        print(f"{'─' * 60}")

        for pid in common_pids:
            md_paths = find_mds_for_problem(exp_dir, pid, max_epochs=n_epochs)
            if not md_paths:
                print(f"  Warning: no .md for problem {pid}")
                continue

            for epoch_idx, md_path in enumerate(md_paths):
                metrics = analyze_one(
                    md_path, cache, client,
                    analysis_template, dedup_template,
                )
                if metrics is not None:
                    metrics["strategy"] = strat_name
                    metrics["problem_id"] = pid
                    metrics["epoch"] = epoch_idx
                    metrics["md_stem"] = Path(md_path).stem
                    all_metrics.append(metrics)

    # Save results
    os.makedirs(output_dir, exist_ok=True)
    results = {
        "model": selected_model,
        "baseline_strategy": "baseline",
        "alt_strategy": strategy,
        "n_problems": len(common_pids),
        "n_epochs": n_epochs,
        "problem_ids": common_pids,
        "metrics": all_metrics,
    }
    results_path = os.path.join(output_dir, "results.json")
    Path(results_path).write_text(json.dumps(results, indent=2))
    print(f"\nSaved: {results_path}")

    print_comparison(results)


# ─── Reporting ──────────────────────────────────────────────────────────────

def _strategy_stats(metrics: list[dict], strat: str) -> dict:
    """Aggregate metrics for a strategy across all problems and epochs."""
    subset = [m for m in metrics if m["strategy"] == strat]
    if not subset:
        return {}
    n = len(subset)
    mean_ideas = sum(m["total_ideas"] for m in subset) / n
    mean_tokens = sum(m["tokens"].get("total_tokens", 0) for m in subset) / n
    ideas_per_ktok = (mean_ideas / (mean_tokens / 1000)) if mean_tokens > 0 else 0

    # Per-category
    all_cats = set()
    for m in subset:
        all_cats.update(m.get("by_category", {}).keys())
    cat_means = {}
    for cat in sorted(all_cats):
        cat_means[cat] = sum(m.get("by_category", {}).get(cat, 0) for m in subset) / n

    return {
        "n": n,
        "mean_ideas": mean_ideas,
        "mean_tokens": mean_tokens,
        "ideas_per_ktok": ideas_per_ktok,
        "cat_means": cat_means,
    }


def print_comparison(results: dict):
    metrics = results["metrics"]
    baseline = results["baseline_strategy"]
    alt = results["alt_strategy"]

    print("\n" + "=" * 70)
    print(f"COMPARISON: {baseline} vs {alt}")
    print(f"Model: {results['model']}  |  "
          f"Problems: {results['n_problems']}  |  Epochs: {results['n_epochs']}")
    print("=" * 70)

    b_stats = _strategy_stats(metrics, baseline)
    a_stats = _strategy_stats(metrics, alt)

    if not b_stats or not a_stats:
        print("  (insufficient data)")
        return

    # Summary
    print(f"\n  {'Strategy':<25s} {'n':>4s} {'Ideas':>7s} {'Tokens':>10s} {'Ideas/kT':>9s}")
    print(f"  {'-' * 60}")
    for label, stats in [(baseline, b_stats), (alt, a_stats)]:
        print(f"  {label:<25s} {stats['n']:>4d} {stats['mean_ideas']:>7.1f} "
              f"{stats['mean_tokens']:>10,.0f} {stats['ideas_per_ktok']:>9.2f}")

    # Boost
    boost = a_stats["mean_ideas"] - b_stats["mean_ideas"]
    pct = (boost / b_stats["mean_ideas"] * 100) if b_stats["mean_ideas"] > 0 else 0
    print(f"\n  Boost: {boost:>+.1f} ideas ({pct:>+.1f}%)")

    # Category breakdown
    all_cats = set(b_stats.get("cat_means", {})) | set(a_stats.get("cat_means", {}))
    leaf_cats = sorted(c for c in all_cats if "." in c)

    if leaf_cats:
        print(f"\n  {'Strategy':<25s}", end="")
        for cat in leaf_cats:
            short = cat.split(".")[-1]
            print(f" {short:>10s}", end="")
        # Also show non-leaf (top-level list) categories
        top_cats = sorted(c for c in all_cats if "." not in c
                          and c not in {lc.split(".")[0] for lc in leaf_cats})
        for cat in top_cats:
            print(f" {cat:>14s}", end="")
        print()
        print(f"  {'-' * (25 + len(leaf_cats) * 11 + len(top_cats) * 15)}")

        for label, stats in [(baseline, b_stats), (alt, a_stats)]:
            print(f"  {label:<25s}", end="")
            for cat in leaf_cats:
                print(f" {stats['cat_means'].get(cat, 0):>10.1f}", end="")
            for cat in top_cats:
                print(f" {stats['cat_means'].get(cat, 0):>14.1f}", end="")
            print()

    # Per-problem detail
    problem_ids = results["problem_ids"]
    print(f"\n  {'Problem':<10s} {'baseline':>10s} {'alt':>10s} {'diff':>8s}")
    print(f"  {'-' * 42}")
    for pid in problem_ids:
        b_vals = [m["total_ideas"] for m in metrics
                  if m["strategy"] == baseline and m["problem_id"] == pid]
        a_vals = [m["total_ideas"] for m in metrics
                  if m["strategy"] == alt and m["problem_id"] == pid]
        b_mean = sum(b_vals) / len(b_vals) if b_vals else 0
        a_mean = sum(a_vals) / len(a_vals) if a_vals else 0
        diff = a_mean - b_mean
        print(f"  {pid:<10} {b_mean:>10.1f} {a_mean:>10.1f} {diff:>+8.1f}")

    print()


# ─── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Compare idea generation breadth: baseline vs alternative strategy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--eval-dir", required=False,
                        help="Directory containing eval result JSON files")
    parser.add_argument("--model", default=None,
                        help="Effective model (e.g. deepseek-chat)")
    parser.add_argument("--strategy", default=None,
                        help="Alternative strategy to compare against baseline")
    parser.add_argument("--n-problems", type=int, default=None,
                        help="Number of problems to analyze (default: all)")
    parser.add_argument("--n-epochs", type=int, default=1,
                        help="Number of epochs per problem (default: 1)")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="Output directory for results.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show plan without running LLM calls")
    parser.add_argument("--print-results", default=None, metavar="PATH",
                        help="Print comparison from existing results.json")
    args = parser.parse_args()

    if args.print_results:
        data = json.loads(Path(args.print_results).read_text())
        print_comparison(data)
        return

    if not args.eval_dir or not args.strategy:
        parser.error("--eval-dir and --strategy are required (unless using --print-results)")

    output_dir = args.output_dir or os.path.join(
        "comparison_output",
        Path(args.eval_dir).name,
        f"baseline_vs_{args.strategy}",
    )

    run_comparison(
        eval_dir=args.eval_dir,
        model=args.model,
        strategy=args.strategy,
        n_problems=args.n_problems,
        n_epochs=args.n_epochs,
        output_dir=output_dir,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
