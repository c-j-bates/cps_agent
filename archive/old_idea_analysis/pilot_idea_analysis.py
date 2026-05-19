"""
Pilot Idea Graph Analysis

For each non-baseline prompting strategy, selects problems for a paired
comparison of idea generation breadth against baseline:

  Group A: baseline failed, but this strategy succeeded
  Group B: both baseline and this strategy failed

Then runs idea_graph_analysis on both baseline and the alternative strategy
for each selected problem, and compares idea breadth between groups.

This tests whether "both-failed" problems correlate with fewer ideas being
generated and less of a boost from structured prompting over baseline.

Usage:
    # Discover available runs and show problem selections (dry run)
    python pilot_idea_analysis.py --eval-dir eval_results/minute-cryptic-par1 --dry-run

    # Run full pipeline
    python pilot_idea_analysis.py --eval-dir eval_results/minute-cryptic-par1

    # Specify model explicitly
    python pilot_idea_analysis.py --eval-dir eval_results/minute-cryptic-par1 --model deepseek-chat

    # Control sample sizes
    python pilot_idea_analysis.py --eval-dir eval_results/minute-cryptic-par1 --n-per-group 5
"""

import argparse
import json
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

from analyze_results import canonical_model, canonical_strategy


# ─── Discovery ───────────────────────────────────────────────────────────────

def load_eval_results(eval_dir: str) -> list[dict]:
    """Load all eval result JSONs from a directory."""
    results = []
    for f in sorted(Path(eval_dir).glob("*.json")):
        data = json.loads(f.read_text())
        data["_file"] = str(f)
        if data.get("samples"):
            results.append(data)
    return results


def resolve_effective_model(result: dict) -> str:
    """Resolve the effective model identity, accounting for thinking."""
    return canonical_model(result["model"], result.get("thinking"))


def discover_runs(eval_dir: str) -> dict:
    """Discover all runs, grouped by effective model.

    Returns {effective_model: {strategy: result_dict}}.
    """
    results = load_eval_results(eval_dir)
    by_model = defaultdict(dict)
    for r in results:
        model = resolve_effective_model(r)
        strategy = canonical_strategy(r["config_name"])
        if strategy not in by_model[model] or \
                len(r["samples"]) > len(by_model[model][strategy]["samples"]):
            by_model[model][strategy] = r
    return dict(by_model)


def print_discovery(discovery: dict):
    """Print discovered runs."""
    print("Discovered runs:")
    for effective_model, by_strategy in sorted(discovery.items()):
        print(f"\n  {effective_model}:")
        for strategy, r in sorted(by_strategy.items()):
            n = len(r["samples"])
            exp_dir = r.get("experiment_log_dir", "?")
            thinking = r.get("thinking", False)
            raw_model = r["model"]
            label = f"{raw_model} (thinking={thinking})" if raw_model != effective_model else effective_model
            print(f"    {strategy:<35s}  n={n:<4d}  {label}")
            print(f"      logs: {exp_dir}")


# ─── Problem Selection (per strategy pair) ───────────────────────────────────

def _build_correctness(result: dict) -> dict[int, bool]:
    """Build {problem_id: solved} from a result. Solved = any epoch correct."""
    by_pid = defaultdict(lambda: False)
    for s in result["samples"]:
        if s.get("correct"):
            by_pid[s["id"]] = True
    return dict(by_pid)


def select_problems_for_pair(
    baseline_result: dict,
    alt_result: dict,
    n_per_group: int = 5,
    seed: int = 42,
) -> dict:
    """Select problems for a baseline-vs-alternative comparison.

    Group A: baseline failed, alternative succeeded
    Group B: both failed
    """
    baseline_correct = _build_correctness(baseline_result)
    alt_correct = _build_correctness(alt_result)

    baseline_pids = {s["id"] for s in baseline_result["samples"]}
    alt_pids = {s["id"] for s in alt_result["samples"]}
    common = baseline_pids & alt_pids

    group_a_pool = []  # baseline failed, alt succeeded
    group_b_pool = []  # both failed

    for pid in sorted(common):
        b = baseline_correct.get(pid, False)
        a = alt_correct.get(pid, False)
        if not b and a:
            group_a_pool.append(pid)
        elif not b and not a:
            group_b_pool.append(pid)

    rng = random.Random(seed)
    group_a = sorted(rng.sample(group_a_pool, min(n_per_group, len(group_a_pool))))
    group_b = sorted(rng.sample(group_b_pool, min(n_per_group, len(group_b_pool))))

    return {
        "group_a": group_a,
        "group_b": group_b,
        "group_a_pool_size": len(group_a_pool),
        "group_b_pool_size": len(group_b_pool),
        "common_problems": len(common),
    }


# ─── Analysis (delegates to shared cache) ────────────────────────────────────


# ─── Main Pipeline ───────────────────────────────────────────────────────────

def run_pilot(eval_dir: str, model: str | None, n_per_group: int,
              output_dir: str, dry_run: bool, seed: int,
              strategies_filter: list[str] | None = None):
    """Main pilot analysis pipeline — one comparison per alternative strategy."""
    discovery = discover_runs(eval_dir)
    print_discovery(discovery)

    # Select model
    available_models = list(discovery.keys())
    if model:
        if model not in discovery:
            print(f"\nError: model '{model}' not found. Available: {available_models}")
            sys.exit(1)
        selected_model = model
    elif len(available_models) == 1:
        selected_model = available_models[0]
    else:
        print(f"\nMultiple models found: {available_models}")
        print("Use --model to select one.")
        sys.exit(1)

    runs = discovery[selected_model]
    if "baseline" not in runs:
        print("Error: no baseline run found")
        sys.exit(1)

    baseline_result = runs["baseline"]
    alt_strategies = sorted(s for s in runs if s != "baseline")
    if strategies_filter:
        unknown = set(strategies_filter) - set(alt_strategies)
        if unknown:
            print(f"Error: unknown strategies {unknown}. Available: {alt_strategies}")
            sys.exit(1)
        alt_strategies = [s for s in alt_strategies if s in strategies_filter]
    if not alt_strategies:
        print("Error: no non-baseline strategies found")
        sys.exit(1)

    print(f"\nSelected model: {selected_model}")
    print(f"Baseline: {len(baseline_result['samples'])} samples")
    print(f"Alternative strategies: {alt_strategies}")

    # Per-strategy comparisons
    all_comparisons = {}

    for alt_strategy in alt_strategies:
        alt_result = runs[alt_strategy]
        selection = select_problems_for_pair(
            baseline_result, alt_result,
            n_per_group=n_per_group, seed=seed,
        )

        print(f"\n{'─' * 70}")
        print(f"  baseline vs {alt_strategy}")
        print(f"  Common problems: {selection['common_problems']}")
        print(f"  Group A (baseline failed, {alt_strategy} succeeded): "
              f"{len(selection['group_a'])} from {selection['group_a_pool_size']} candidates")
        print(f"    IDs: {selection['group_a']}")
        print(f"  Group B (both failed): "
              f"{len(selection['group_b'])} from {selection['group_b_pool_size']} candidates")
        print(f"    IDs: {selection['group_b']}")

        all_comparisons[alt_strategy] = {
            "selection": selection,
            "metrics": [],  # filled in below
        }

    if dry_run:
        print("\n[Dry run — stopping before LLM calls]")
        return

    # Run analyses — artifacts go into per-experiment-dir shared caches
    from idea_graph_analysis import (
        _load_template, _make_client, analyze_one,
        find_mds_for_problem, cache_dir_for,
    )

    analysis_template = _load_template("idea_graph_analysis_prompt.txt")
    dedup_template = _load_template("idea_graph_dedup_prompt.txt")
    client = _make_client()

    os.makedirs(output_dir, exist_ok=True)
    baseline_exp_dir = baseline_result.get("experiment_log_dir", "")

    for alt_strategy in alt_strategies:
        comp = all_comparisons[alt_strategy]
        selection = comp["selection"]
        alt_result = runs[alt_strategy]
        alt_exp_dir = alt_result.get("experiment_log_dir", "")
        all_pids = selection["group_a"] + selection["group_b"]

        print(f"\n{'═' * 70}")
        print(f"ANALYZING: baseline vs {alt_strategy}")
        print(f"{'═' * 70}")

        for pid in all_pids:
            group = "A" if pid in selection["group_a"] else "B"

            for strategy, exp_dir in [("baseline", baseline_exp_dir),
                                      (alt_strategy, alt_exp_dir)]:
                md_paths = find_mds_for_problem(exp_dir, pid, max_epochs=1)
                if not md_paths:
                    print(f"  Warning: no .md for {strategy} problem {pid}")
                    continue

                md_path = md_paths[0]
                cache = cache_dir_for(exp_dir)
                metrics = analyze_one(
                    md_path, cache, client,
                    analysis_template, dedup_template,
                )
                if metrics is not None:
                    metrics["group"] = group
                    metrics["strategy"] = strategy
                    metrics["problem_id"] = pid
                    metrics["md_stem"] = Path(md_path).stem
                    comp["metrics"].append(metrics)

    # Save results
    pilot_results = {
        "model": selected_model,
        "comparisons": {
            alt: {
                "selection": comp["selection"],
                "metrics": comp["metrics"],
            }
            for alt, comp in all_comparisons.items()
        },
    }
    results_path = os.path.join(output_dir, "pilot_results.json")
    Path(results_path).write_text(json.dumps(pilot_results, indent=2))
    print(f"\nSaved: {results_path}")

    print_comparison(pilot_results)


# ─── Comparison ──────────────────────────────────────────────────────────────

def _group_means(metrics: list[dict], strategy: str, group: str) -> dict | None:
    """Compute means for a (strategy, group) slice. Returns None if empty."""
    subset = [m for m in metrics if m["strategy"] == strategy and m["group"] == group]
    if not subset:
        return None
    n = len(subset)
    mean_ideas = sum(m["total_ideas"] for m in subset) / n
    mean_tokens = sum(m["tokens"].get("total_tokens", 0) for m in subset) / n
    ideas_per_ktok = (mean_ideas / (mean_tokens / 1000)) if mean_tokens > 0 else 0

    cat_means = {}
    all_cats = set()
    for m in subset:
        all_cats.update(m.get("by_category", {}).keys())
    for cat in all_cats:
        cat_means[cat] = sum(m.get("by_category", {}).get(cat, 0) for m in subset) / n

    return {
        "n": n,
        "mean_ideas": mean_ideas,
        "mean_tokens": mean_tokens,
        "ideas_per_ktok": ideas_per_ktok,
        "cat_means": cat_means,
    }


def print_comparison(pilot_results: dict):
    """Print per-strategy comparison tables."""
    comparisons = pilot_results.get("comparisons", {})
    if not comparisons:
        print("\nNo comparisons to display.")
        return

    print("\n" + "=" * 80)
    print("PILOT COMPARISON: Idea Generation Breadth")
    print(f"Model: {pilot_results['model']}")
    print("=" * 80)

    for alt_strategy, comp in sorted(comparisons.items()):
        metrics = comp["metrics"]
        selection = comp["selection"]
        if not metrics:
            continue

        print(f"\n{'─' * 70}")
        print(f"  baseline vs {alt_strategy}")
        print(f"  Group A ({len(selection['group_a'])} problems): "
              f"baseline failed, {alt_strategy} succeeded — IDs: {selection['group_a']}")
        print(f"  Group B ({len(selection['group_b'])} problems): "
              f"both failed — IDs: {selection['group_b']}")
        print(f"{'─' * 70}")

        # Summary table
        print(f"\n  {'Strategy':<30s} {'Group':>6s} {'Ideas':>7s} {'Tokens':>10s} "
              f"{'Ideas/kT':>9s}  {'n':>3s}")
        print(f"  {'-' * 72}")

        for strategy in ["baseline", alt_strategy]:
            for group in ["A", "B"]:
                stats = _group_means(metrics, strategy, group)
                if not stats:
                    continue
                print(f"  {strategy:<30s} {group:>6s} {stats['mean_ideas']:>7.1f} "
                      f"{stats['mean_tokens']:>10,.0f} {stats['ideas_per_ktok']:>9.2f}  "
                      f"{stats['n']:>3d}")

        # Boost
        for group in ["A", "B"]:
            base = _group_means(metrics, "baseline", group)
            alt = _group_means(metrics, alt_strategy, group)
            if base and alt:
                boost = alt["mean_ideas"] - base["mean_ideas"]
                pct = (boost / base["mean_ideas"] * 100) if base["mean_ideas"] > 0 else 0
                print(f"\n  Boost (Group {group}): {boost:>+.1f} ideas ({pct:>+.1f}%)")

        # Category breakdown
        all_cats = set()
        for m in metrics:
            all_cats.update(m.get("by_category", {}).keys())
        leaf_cats = sorted(c for c in all_cats if "." in c)

        if leaf_cats:
            print(f"\n  {'Strategy':<22s} {'Grp':>4s}", end="")
            for cat in leaf_cats:
                short = cat.split(".")[-1]
                print(f" {short:>10s}", end="")
            print()
            print(f"  {'-' * (30 + len(leaf_cats) * 11)}")

            for strategy in ["baseline", alt_strategy]:
                for group in ["A", "B"]:
                    stats = _group_means(metrics, strategy, group)
                    if not stats:
                        continue
                    print(f"  {strategy:<22s} {group:>4s}", end="")
                    for cat in leaf_cats:
                        print(f" {stats['cat_means'].get(cat, 0):>10.1f}", end="")
                    print()

    print()


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Pilot Idea Graph Analysis — per-strategy paired comparison",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--eval-dir", required=True,
                        help="Directory containing eval result JSON files")
    parser.add_argument("--model", default=None,
                        help="Effective model to analyze (e.g. deepseek-chat)")
    parser.add_argument("-n", "--n-per-group", type=int, default=5,
                        help="Problems per group per strategy comparison (default: 5)")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="Output directory (default: pilot_analysis/<eval-dir-basename>)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show discovery and selections without LLM calls")
    parser.add_argument("--strategies", nargs="+", default=None, metavar="NAME",
                        help="Only compare these strategies against baseline "
                             "(e.g. --strategies generate_vars step_back)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for problem sampling (default: 42)")
    parser.add_argument("--print-results", default=None, metavar="PATH",
                        help="Print comparison from existing pilot_results.json")
    args = parser.parse_args()

    if args.print_results:
        data = json.loads(Path(args.print_results).read_text())
        print_comparison(data)
        return

    output_dir = args.output_dir or os.path.join(
        "pilot_analysis", Path(args.eval_dir).name
    )

    run_pilot(
        eval_dir=args.eval_dir,
        model=args.model,
        n_per_group=args.n_per_group,
        output_dir=output_dir,
        dry_run=args.dry_run,
        seed=args.seed,
        strategies_filter=args.strategies,
    )


if __name__ == "__main__":
    main()
