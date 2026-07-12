"""
Coding Analysis — CLI Entry Point.

Implements the full pipeline from Section 13 of the spec:
  1. Code solver outputs for each (strategy, clue) pair
  2. Batch review novel mechanism types
  3. Run consistency checks
  4. Compute Jaccard distances and TDA persistence
  5. Extract features and aggregate across clues
  6. Compare strategies and generate visualizations

Usage:
    # Full pipeline for one experiment group (all strategies for a dataset)
    python -m coding_analysis_minute_cryptic.run_coding experiment_logs/minute-cryptic-par1

    # Specify output directory
    python -m coding_analysis_minute_cryptic.run_coding experiment_logs/minute-cryptic-par1 \\
        --output-dir coding_results/minute-cryptic-par1

    # Code only (skip TDA and comparison — useful for incremental work)
    python -m coding_analysis_minute_cryptic.run_coding experiment_logs/minute-cryptic-par1 --code-only

    # Features only (skip coding, use cached traces)
    python -m coding_analysis_minute_cryptic.run_coding experiment_logs/minute-cryptic-par1 --features-only

    # Specific strategies
    python -m coding_analysis_minute_cryptic.run_coding experiment_logs/minute-cryptic-par1 \\
        --strategies baseline keep_thinking_step_by_step self_discover
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from .coding import (
    code_experiment,
    collect_novel_types,
    review_novel_types,
    apply_novel_type_decisions,
    extract_problem_id,
)
from .consistency import check_consistency
from .features import extract_features, extract_features_pooled, beta_diversity
from .ideas import (
    build_trees,
    extract_leaf_ideas,
    deduplicate_ideas,
    idea_to_frozenset,
)
from .regression import run_regression_analysis
from .gold_analysis import run_gold_analysis
from .comparison import (
    aggregate_features,
    compute_beta_diversity_matrix,
    save_comparison,
    print_summary_table,
)


def parse_strategy_and_model(dirname: str) -> tuple[str | None, str | None]:
    """Extract (strategy, model) from an experiment log directory name.

    Expected format: {timestamp}_config_{dataset}_{strategy}_{model}
    where model always contains at least one hyphen.

    Example: 20260319_111404_config_minute_cryptic_baseline_claude-opus-4-6-instant
             -> ("baseline", "claude-opus-4-6-instant")
    """
    # Strip timestamp prefix: "20260319_111404_" -> "config_minute_cryptic_..."
    m = re.match(r'\d+_\d+_(config_.+)$', dirname)
    if not m:
        return None, None

    rest = m.group(1)  # "config_minute_cryptic_baseline_claude-opus-4-6-instant"

    # The model name is always the last underscore-delimited segment that
    # contains a hyphen. Split from the right to find it.
    parts = rest.split("_")
    model_start = None
    for i in range(len(parts) - 1, -1, -1):
        if "-" in parts[i]:
            model_start = i
        else:
            break

    if model_start is None:
        return None, None

    model = "_".join(parts[model_start:])
    config_part = "_".join(parts[:model_start])  # "config_minute_cryptic_baseline"

    # Strip "config_" prefix and known dataset prefixes
    config_part = re.sub(r'^config_', '', config_part)
    for prefix in ["minute_cryptic_", "rosetta_", "bigbench_rosetta_"]:
        if config_part.startswith(prefix):
            config_part = config_part[len(prefix):]
            break

    strategy = config_part or None
    return strategy, model


def parse_strategy_from_dirname(dirname: str) -> str | None:
    """Extract the strategy name from an experiment log directory name."""
    strategy, _ = parse_strategy_and_model(dirname)
    return strategy


def find_experiment_dirs(experiment_group_dir: str,
                         strategies: list[str] | None = None,
                         models: list[str] | None = None,
                         ) -> dict[str, list[str]]:
    """Find experiment log directories grouped by strategy.

    Returns {strategy_name: [experiment_log_dir, ...]}.
    Includes multiple dirs per strategy if there are multiple models/runs.

    Args:
        strategies: If set, only include these strategy names.
        models: If set, only include dirs whose model name contains one of
                these substrings (e.g. ["claude-opus-4-6", "deepseek-chat"]).
    """
    group_dir = Path(experiment_group_dir)
    result = defaultdict(list)

    for subdir in sorted(group_dir.iterdir()):
        if not subdir.is_dir():
            continue
        strategy, model = parse_strategy_and_model(subdir.name)
        if strategy is None:
            continue
        if strategies and strategy not in strategies:
            continue
        if models and model and not any(m in model for m in models):
            continue
        result[strategy].append(str(subdir))

    return dict(result)


def load_cached_results(output_dir: str, experiment_name: str,
                        max_problems: int | None = None) -> list[dict]:
    """Load previously cached coding results for an experiment."""
    cache_dir = os.path.join(output_dir, experiment_name, "coding")
    if not os.path.isdir(cache_dir):
        return []

    results = []
    for f in sorted(Path(cache_dir).glob("*_cleaned.json")):
        data = json.loads(f.read_text())
        results.append(data)
        if max_problems and len(results) >= max_problems:
            break
    return results


def _coding_output_dir(output_dir: str, coder_model: str) -> str:
    """Return the coded-by-<model> subdirectory for coding outputs."""
    return os.path.join(output_dir, f"coded-by-{coder_model}")


def _covered_by_decisions(decisions: list[dict]) -> set[str]:
    """Return the set of novel-type names a Prompt-C decision list covers.

    Used to detect cache staleness: if `collect_novel_types` returns
    names that aren't in this set, the cache is incomplete (a later
    batch of coded problems introduced new novel types).
    """
    covered = set()
    for d in decisions:
        action = d.get("action")
        if action in ("remap", "decompose") and "from" in d:
            covered.add(d["from"])
        elif action == "merge":
            covered.update(d.get("types", []))
        elif action == "keep" and "type" in d:
            covered.add(d["type"])
    return covered


def run_pipeline(experiment_group_dir: str,
                 output_dir: str,
                 strategies: list[str] | None = None,
                 models: list[str] | None = None,
                 max_problems: int | None = None,
                 code_only: bool = False,
                 features_only: bool = False,
                 no_viz: bool = False,
                 no_api: bool = False,
                 refresh_novel_types: bool = False,
                 model: str = "claude-opus-4-6",
                 provider: str = "claude",
                 thinking: bool | str = False):
    """Run the full coding analysis pipeline.

    Args:
        models: If set, only include experiment dirs whose model name
                contains one of these substrings.
        max_problems: If set, only process the first N problems per
                      experiment directory.
        no_viz: If True, skip visualization generation.
        no_api: If True, refuse to make any LLM API calls. Coding and
                novel-type review fall back to cached results; missing
                caches are skipped with a warning instead of triggering
                an API call. Implies ``features_only`` for the coding step.
    """
    experiment_group_dir = os.path.abspath(experiment_group_dir)
    group_name = Path(experiment_group_dir).name

    if not output_dir:
        output_dir = os.path.join("coding_results", group_name)
    os.makedirs(output_dir, exist_ok=True)

    # ── Step 1: Find experiment directories ─────────────────────────────
    strategy_dirs = find_experiment_dirs(experiment_group_dir, strategies,
                                         models=models)
    if not strategy_dirs:
        print(f"No experiment directories found in {experiment_group_dir}")
        return

    print(f"Found strategies: {', '.join(sorted(strategy_dirs.keys()))}")
    for strategy, dirs in sorted(strategy_dirs.items()):
        print(f"  {strategy}: {len(dirs)} run(s)")

    # ── Step 2: Code all (strategy, clue) pairs ─────────────────────────
    # {strategy: {experiment_name: [coded_results]}}
    all_coded = defaultdict(dict)
    client = None

    # Coding outputs go under coded-by-<model>/ so different coder LLMs
    # produce independent caches.
    coding_output_dir = _coding_output_dir(output_dir, model)
    os.makedirs(coding_output_dir, exist_ok=True)
    print(f"Coding output dir: {coding_output_dir}")

    for strategy, exp_dirs in sorted(strategy_dirs.items()):
        print(f"\n{'='*60}")
        print(f"Strategy: {strategy}")
        print(f"{'='*60}")

        for exp_dir in exp_dirs:
            exp_name = Path(exp_dir).name

            if features_only or no_api:
                results = load_cached_results(coding_output_dir, exp_name,
                                             max_problems=max_problems)
                # Fallback: try old layout (no coded-by-* prefix)
                if not results:
                    results = load_cached_results(output_dir, exp_name,
                                                 max_problems=max_problems)
                if results:
                    print(f"  Loaded {len(results)} cached results for {exp_name}")
                    all_coded[strategy][exp_name] = results
                    continue
                if no_api:
                    print(f"  [no-api] No cached results for {exp_name}; "
                          f"skipping (would have required LLM coding)")
                    continue

            results = code_experiment(exp_dir, coding_output_dir, client=client,
                                     max_problems=max_problems,
                                     coder_model=model,
                                     coder_provider=provider,
                                     coder_thinking=thinking)
            all_coded[strategy][exp_name] = results

    if code_only:
        print(f"\n  Coding complete. Results cached in {output_dir}/")
        return

    # ── Step 3: Batch novel type review ─────────────────────────────────
    all_results_flat = [
        r for strat_runs in all_coded.values()
        for results in strat_runs.values()
        for r in results
    ]

    novel_types = collect_novel_types(all_results_flat)
    approved_novel = set()

    decisions_path = os.path.join(coding_output_dir, "novel_type_decisions.json")

    if novel_types:
        print(f"\n── Novel type review ({len(novel_types)} types) ──")
        observed = {t["type"] for t in novel_types}

        cached_decisions: list[dict] | None = None
        if os.path.isfile(decisions_path):
            cached_decisions = json.loads(Path(decisions_path).read_text())

        # Detect cache staleness: which observed types is the cache missing,
        # and which cached types are no longer present in the dataset?
        cache_is_stale = False
        new_types: set[str] = set()
        stale_types: set[str] = set()
        if cached_decisions is not None:
            covered = _covered_by_decisions(cached_decisions)
            new_types = observed - covered
            stale_types = covered - observed
            cache_is_stale = bool(new_types) or bool(stale_types)

        force_refresh = refresh_novel_types or cache_is_stale

        if cached_decisions is not None and not force_refresh:
            decisions = cached_decisions
            print(f"  Using cached decisions: {decisions_path}")
        elif no_api:
            if cached_decisions is None:
                print(f"  [no-api] No cached novel-type decisions at "
                      f"{decisions_path}; skipping review (all novel "
                      f"types treated as rejected)")
                decisions = []
            else:
                if new_types:
                    print(f"  [no-api] WARNING: cache is missing decisions "
                          f"for {len(new_types)} new novel type(s): "
                          f"{sorted(new_types)[:5]}"
                          f"{'…' if len(new_types) > 5 else ''}")
                if stale_types:
                    print(f"  [no-api] WARNING: cache contains stale "
                          f"decisions for {len(stale_types)} type(s) not "
                          f"in current dataset: {sorted(stale_types)[:5]}"
                          f"{'…' if len(stale_types) > 5 else ''}")
                print(f"  [no-api] Using stale cache as-is. To refresh: "
                      f"re-run without --no-api (or pass "
                      f"--refresh-novel-types).")
                decisions = cached_decisions
        else:
            if cached_decisions is not None:
                reasons = []
                if refresh_novel_types:
                    reasons.append("--refresh-novel-types flag")
                if new_types:
                    reasons.append(
                        f"{len(new_types)} new novel type(s) not in cache: "
                        f"{sorted(new_types)[:5]}"
                        f"{'…' if len(new_types) > 5 else ''}"
                    )
                if stale_types:
                    reasons.append(
                        f"{len(stale_types)} cached type(s) no longer "
                        f"observed: {sorted(stale_types)[:5]}"
                        f"{'…' if len(stale_types) > 5 else ''}"
                    )
                print(f"  Cache at {decisions_path} is stale; "
                      f"re-running PROMPT_C from scratch.")
                for r in reasons:
                    print(f"    • {r}")

            if client is None:
                from llm_clients import create_client
                client = create_client(provider, model=model,
                                       thinking=thinking, temperature=0.3)

            decisions = review_novel_types(novel_types, client=client)
            Path(decisions_path).write_text(json.dumps(decisions, indent=2))
            print(f"  Saved decisions: {decisions_path}")

        apply_novel_type_decisions(all_results_flat, decisions)

        # Track approved novel types
        for d in decisions:
            if d.get("action") == "keep":
                approved_novel.add(d["type"])
    else:
        print("\n  No novel mechanism types found.")

    # ── Step 4: Consistency checks ──────────────────────────────────────
    print("\n── Consistency checks ──")
    all_warnings = []
    for result in all_results_flat:
        warnings = check_consistency(result, approved_novel)
        if warnings:
            pid = result.get("problem_id", "?")
            clue = result.get("clue_text", "?")[:40]
            all_warnings.extend([f"Problem {pid} ({clue}...): {w}" for w in warnings])

    if all_warnings:
        print(f"  {len(all_warnings)} warnings:")
        for w in all_warnings[:20]:
            print(f"    {w}")
        if len(all_warnings) > 20:
            print(f"    ... and {len(all_warnings) - 20} more")
    else:
        print("  All checks passed.")

    # ── Step 5: Extract features per (strategy, clue) ───────────────────
    print("\n── Feature extraction ──")
    # Organize: {strategy: {problem_id: coded_result}}
    # For multiple runs of same strategy, take the first one per problem
    strategy_by_problem = defaultdict(dict)
    for strategy, exp_runs in all_coded.items():
        for exp_name, results in exp_runs.items():
            for r in results:
                pid = r.get("problem_id")
                if pid is not None and pid not in strategy_by_problem[strategy]:
                    strategy_by_problem[strategy][pid] = r

    # Compute features and accuracy
    strategy_features = {}  # {strategy: {problem_id: features}}
    strategy_ideas = {}     # {strategy: {problem_id: [frozensets]}}
    strategy_accuracy = {}  # {strategy: {"correct": int, "total": int, "pct": float}}

    for strategy, by_problem in sorted(strategy_by_problem.items()):
        print(f"  {strategy}: {len(by_problem)} problems")
        features_by_clue = {}
        ideas_by_clue = {}
        n_correct = 0
        n_total = 0

        for pid, result in sorted(by_problem.items()):
            features = extract_features(result)
            features_by_clue[pid] = features

            # Collect idea frozensets for beta diversity
            trace = result.get("trace", [])
            trees = build_trees(trace)
            leaves = deduplicate_ideas(extract_leaf_ideas(trees))
            ideas_by_clue[pid] = [idea_to_frozenset(l) for l in leaves if idea_to_frozenset(l)]

            # Track accuracy
            solver_ans = result.get("solver_answer")
            correct_sol = result.get("correct_solution")
            if correct_sol:
                n_total += 1
                if solver_ans and solver_ans.upper() == correct_sol.upper():
                    n_correct += 1

        strategy_features[strategy] = features_by_clue
        strategy_ideas[strategy] = ideas_by_clue
        pct = (n_correct / n_total * 100) if n_total > 0 else 0.0
        strategy_accuracy[strategy] = {
            "correct": n_correct, "total": n_total, "pct": pct
        }

    # ── Step 6: Aggregate and compare ───────────────────────────────────
    print("\n── Aggregation and comparison ──")
    strategy_profiles = {}
    for strategy, features_by_clue in strategy_features.items():
        strategy_profiles[strategy] = aggregate_features(features_by_clue)

    # Beta diversity
    beta_matrix = compute_beta_diversity_matrix(strategy_ideas)

    # Save everything — all downstream outputs scoped by coder model
    comparison_dir = os.path.join(coding_output_dir, "comparison")
    save_comparison(strategy_profiles, beta_matrix, comparison_dir,
                    accuracy=strategy_accuracy)

    # Save per-problem features
    per_problem_path = os.path.join(coding_output_dir, "features_per_problem.json")
    serializable = {
        strategy: {
            str(pid): features
            for pid, features in by_problem.items()
        }
        for strategy, by_problem in strategy_features.items()
    }
    Path(per_problem_path).write_text(json.dumps(serializable, indent=2))
    print(f"  Saved per-problem features: {per_problem_path}")

    # ── Step 7: Logistic regression analysis ─────────────────────────────
    print("\n── Logistic regression: features → solve success ──")
    regression_dir = os.path.join(coding_output_dir, "regression")
    run_regression_analysis(strategy_features, strategy_by_problem, regression_dir)

    # ── Step 8: Gold idea analysis ────────────────────────────────────────
    print("\n── Gold idea facet coverage ──")
    # Gold source (hand-coded) lives at top level; coverage output is per-coder
    gold_source_dir = os.path.join(output_dir, "gold_analysis")
    gold_output_dir = os.path.join(coding_output_dir, "gold_analysis")
    os.makedirs(gold_output_dir, exist_ok=True)
    # Symlink the gold source file into the output dir so run_gold_analysis
    # can find it, without duplicating the hand-coded data
    gold_src = os.path.join(gold_source_dir, "gold_ideas_opus.json")
    gold_dst = os.path.join(gold_output_dir, "gold_ideas_opus.json")
    if os.path.isfile(gold_src) and not os.path.exists(gold_dst):
        os.symlink(os.path.abspath(gold_src), gold_dst)
    run_gold_analysis(strategy_by_problem, gold_output_dir)

    # ── Step 9: Visualizations ──────────────────────────────────────────
    if not no_viz:
        print("\n── Generating visualizations ──")
        from .visualize import visualize_coded_results
        for strategy, exp_runs in all_coded.items():
            for exp_name, results in exp_runs.items():
                coding_dir = os.path.join(coding_output_dir, exp_name, "coding")
                vis_dir = os.path.join(coding_output_dir, exp_name, "visualizations")
                # Extract model from experiment dir name
                _, exp_model = parse_strategy_and_model(exp_name)
                if os.path.isdir(coding_dir):
                    visualize_coded_results(
                        coding_dir, vis_dir,
                        strategy=strategy,
                        model=exp_model or "",
                        experiment_name=exp_name,
                    )

    print(f"\n  Done. Results in {coding_output_dir}/")


def main():
    parser = argparse.ArgumentParser(
        description="Coding Analysis Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full pipeline
  python -m coding_analysis_minute_cryptic experiment_logs/minute-cryptic-par1

  # Only code (no TDA/comparison)
  python -m coding_analysis_minute_cryptic experiment_logs/minute-cryptic-par1 --code-only

  # Use cached traces, just compute features
  python -m coding_analysis_minute_cryptic experiment_logs/minute-cryptic-par1 --features-only

  # Filter by strategy and solver model, first 5 puzzles only
  python -m coding_analysis_minute_cryptic experiment_logs/minute-cryptic-par1 \\
      --strategies baseline self_discover \\
      --models claude-opus-4-6-instant \\
      --max-problems 5
        """,
    )
    parser.add_argument("experiment_group_dir",
                        help="Path to the experiment log group directory "
                             "(e.g., experiment_logs/minute-cryptic-par1)")
    parser.add_argument("--output-dir", "-o", default=None,
                        help="Output directory (default: coding_results/<group_name>)")
    parser.add_argument("--strategies", nargs="+", default=None,
                        help="Only process these strategies")
    parser.add_argument("--models", nargs="+", default=None,
                        help="Only process experiments run with these solver "
                             "models (substring match, e.g. 'claude-opus-4-6-instant' "
                             "'deepseek-chat')")
    parser.add_argument("--max-problems", type=int, default=None,
                        help="Only process the first N puzzles per experiment")
    parser.add_argument("--code-only", action="store_true",
                        help="Only run coding (Prompts A+B), skip TDA and comparison")
    parser.add_argument("--features-only", action="store_true",
                        help="Skip coding (use cached traces), run TDA and comparison")
    parser.add_argument("--no-api", action="store_true",
                        help="Refuse to make any LLM API calls. Uses cached "
                             "coding and novel-type decisions only; experiment "
                             "dirs with no cache are skipped with a warning. "
                             "Implies --features-only for the coding step.")
    parser.add_argument("--refresh-novel-types", action="store_true",
                        help="Force Prompt C to re-run from scratch, even if "
                             "a cached novel_type_decisions.json exists. "
                             "Useful when you've coded more problems and want "
                             "MERGE to see all novel types at once. "
                             "Cache staleness (cached decisions vs. currently "
                             "observed novel types) auto-triggers this "
                             "anyway — this flag is for forcing it manually.")
    parser.add_argument("--coder-model", default="claude-opus-4-6",
                        help="LLM model used for coding/analysis calls "
                             "(default: claude-opus-4-6)")
    parser.add_argument("--coder-provider", default="claude",
                        help="LLM provider for coding calls (default: claude)")
    parser.add_argument("--coder-thinking", default="false",
                        help="Enable thinking/reasoning for coder LLM. "
                             "'true' to enable, 'false' to disable (default), "
                             "or an effort level like 'low'/'medium'/'high'.")
    parser.add_argument("--no-viz", action="store_true",
                        help="Skip visualization generation")

    args = parser.parse_args()

    # Parse thinking arg: "true"→True, "false"→False, else str (effort level)
    thinking_val = args.coder_thinking.lower()
    if thinking_val == "true":
        thinking_val = True
    elif thinking_val == "false":
        thinking_val = False
    # else keep as string (effort level like "low"/"medium"/"high")

    run_pipeline(
        experiment_group_dir=args.experiment_group_dir,
        output_dir=args.output_dir,
        strategies=args.strategies,
        models=args.models,
        max_problems=args.max_problems,
        code_only=args.code_only,
        features_only=args.features_only,
        no_viz=args.no_viz,
        no_api=args.no_api,
        refresh_novel_types=args.refresh_novel_types,
        model=args.coder_model,
        provider=args.coder_provider,
        thinking=thinking_val,
    )


if __name__ == "__main__":
    main()
