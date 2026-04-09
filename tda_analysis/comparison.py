"""
Cross-solver comparison and visualization (Section 12 of spec).

Aggregates features across clues, produces radar charts and summary tables.
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from pathlib import Path

import numpy as np

from .features import beta_diversity
from .ideas import idea_to_frozenset, build_trees, extract_leaf_ideas, deduplicate_ideas

# Feature names matching spec Section 9
FEATURE_NAMES = [
    "n_ideas",
    "h0_mean_death", "h0_cv", "h0_entropy",
    "h1_total_pers", "h1_max_pers", "h1_count",
    "n_root_branches", "max_local_fanout", "completion_ratio",
    "mean_depth", "subtree_overlap",
    "parse_entropy", "mechanism_entropy", "execution_entropy", "output_entropy",
    "mean_nn_jaccard", "diameter",
]

# Subset for radar charts (skip highly correlated / hard-to-interpret ones)
RADAR_FEATURES = [
    "n_ideas",
    "h0_mean_death", "h0_cv",
    "h1_total_pers",
    "n_root_branches", "max_local_fanout", "completion_ratio",
    "parse_entropy", "mechanism_entropy",
    "mean_nn_jaccard", "diameter",
]


def aggregate_features(features_by_clue: dict[int, dict]) -> dict:
    """Aggregate features across clues: mean ± SE.

    Input: {problem_id: features_dict}
    Returns: {feature_name: {"mean": float, "se": float, "values": list}}
    """
    if not features_by_clue:
        return {}

    agg = {}
    for feat in FEATURE_NAMES:
        values = [
            f[feat] for f in features_by_clue.values()
            if feat in f and f[feat] is not None
        ]
        if values:
            mean = sum(values) / len(values)
            se = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
            if len(values) > 1:
                se = se / (len(values) ** 0.5)
            agg[feat] = {"mean": mean, "se": se, "values": values}
        else:
            agg[feat] = {"mean": 0.0, "se": 0.0, "values": []}

    return agg


def compute_beta_diversity_matrix(
    strategy_ideas: dict[str, dict[int, list[frozenset]]]
) -> dict:
    """Compute pairwise beta diversity between strategies.

    Input: {strategy: {problem_id: [frozensets]}}
    Returns: {(strategy_a, strategy_b): {"mean": float, "se": float}}
    """
    strategies = sorted(strategy_ideas.keys())
    results = {}

    for i, sa in enumerate(strategies):
        for j, sb in enumerate(strategies):
            if j <= i:
                continue
            # Find shared clues
            shared = set(strategy_ideas[sa].keys()) & set(strategy_ideas[sb].keys())
            if not shared:
                results[(sa, sb)] = {"mean": 0.0, "se": 0.0}
                continue

            betas = []
            for pid in shared:
                b = beta_diversity(strategy_ideas[sa][pid], strategy_ideas[sb][pid])
                betas.append(b)

            mean = sum(betas) / len(betas)
            se = 0.0
            if len(betas) > 1:
                var = sum((v - mean) ** 2 for v in betas) / (len(betas) - 1)
                se = (var / len(betas)) ** 0.5
            results[(sa, sb)] = {"mean": mean, "se": se}

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Visualization
# ═══════════════════════════════════════════════════════════════════════════════

def plot_radar(strategy_profiles: dict[str, dict], output_path: str,
               accuracy: dict[str, dict] | None = None):
    """Radar chart of strategy profiles (Section 12.1).

    Input: {strategy_name: aggregated_features}
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  Warning: matplotlib not installed, skipping radar chart")
        return

    strategies = sorted(strategy_profiles.keys())
    features_to_plot = [
        f for f in FEATURE_NAMES
        if any(f in strategy_profiles[s] and strategy_profiles[s][f]["mean"] != 0
               for s in strategies)
    ]

    if not features_to_plot:
        return

    n_features = len(features_to_plot)
    angles = np.linspace(0, 2 * np.pi, n_features, endpoint=False).tolist()
    angles += angles[:1]  # close the polygon

    fig, ax = plt.subplots(figsize=(10, 10), subplot_kw=dict(polar=True))

    # Normalize each feature to [0, 1] across strategies for visibility
    max_vals = {}
    for f in features_to_plot:
        vals = [strategy_profiles[s].get(f, {}).get("mean", 0) for s in strategies]
        max_vals[f] = max(abs(v) for v in vals) if vals else 1.0
        if max_vals[f] == 0:
            max_vals[f] = 1.0

    colors = ["#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B3",
              "#937860", "#DA8BC3", "#8C8C8C", "#CCB974", "#64B5CD"]

    for i, strategy in enumerate(strategies):
        values = [
            strategy_profiles[strategy].get(f, {}).get("mean", 0) / max_vals[f]
            for f in features_to_plot
        ]
        values += values[:1]
        color = colors[i % len(colors)]
        ax.plot(angles, values, 'o-', linewidth=2, label=strategy, color=color)
        ax.fill(angles, values, alpha=0.1, color=color)

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(features_to_plot, fontsize=8)
    ax.set_title("Strategy Profiles (normalized)", fontsize=14, pad=20)
    ax.legend(loc="upper right", bbox_to_anchor=(1.3, 1.1), fontsize=9)

    # Accuracy text box
    if accuracy:
        acc_lines = ["Accuracy"]
        for s in strategies:
            a = accuracy.get(s, {})
            correct = a.get("correct", 0)
            total = a.get("total", 0)
            acc_lines.append(f"  {s}: {correct}/{total}")
        acc_text = "\n".join(acc_lines)
        fig.text(0.02, 0.02, acc_text, fontsize=8, family="monospace",
                 verticalalignment="bottom",
                 bbox=dict(boxstyle="round,pad=0.4", facecolor="white",
                           edgecolor="#cccccc", alpha=0.9))

    plt.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved radar chart: {output_path}")


def plot_feature_bars(strategy_profiles: dict[str, dict], output_dir: str):
    """Bar charts for each feature across strategies."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    strategies = sorted(strategy_profiles.keys())
    colors = ["#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B3"]

    for feat in FEATURE_NAMES:
        means = [strategy_profiles[s].get(feat, {}).get("mean", 0)
                 for s in strategies]
        ses = [strategy_profiles[s].get(feat, {}).get("se", 0)
               for s in strategies]

        if all(m == 0 for m in means):
            continue

        fig, ax = plt.subplots(figsize=(max(6, len(strategies) * 1.5), 4))
        x = range(len(strategies))
        ax.bar(x, means, yerr=ses, color=[colors[i % len(colors)]
               for i in range(len(strategies))],
               edgecolor="white", linewidth=0.5, capsize=4)
        ax.set_xticks(x)
        ax.set_xticklabels(strategies, fontsize=9, rotation=30, ha="right")
        ax.set_ylabel(feat)
        ax.set_title(f"{feat} by Strategy")
        plt.tight_layout()

        path = os.path.join(output_dir, f"feature_{feat}.png")
        fig.savefig(path, dpi=150)
        plt.close(fig)


def print_summary_table(strategy_profiles: dict[str, dict],
                        accuracy: dict[str, dict] | None = None) -> str:
    """Print a formatted summary table of strategy profiles."""
    strategies = sorted(strategy_profiles.keys())

    lines = ["\n── TDA Creativity Feature Profiles ──\n"]

    # Header
    header = f"{'Feature':<25s}"
    for s in strategies:
        header += f"  {s:>20s}"
    lines.append(header)
    lines.append("─" * len(header))

    for feat in FEATURE_NAMES:
        row = f"{feat:<25s}"
        for s in strategies:
            prof = strategy_profiles[s].get(feat, {})
            mean = prof.get("mean", 0)
            se = prof.get("se", 0)
            row += f"  {mean:>8.3f} ± {se:<8.3f}"
        lines.append(row)

    # Accuracy section
    if accuracy:
        lines.append("")
        lines.append("── Accuracy ──")
        lines.append("")
        for s in strategies:
            a = accuracy.get(s, {})
            correct = a.get("correct", 0)
            total = a.get("total", 0)
            lines.append(f"  {s:<23s}  {correct}/{total}")

    table = "\n".join(lines)
    print(table)
    return table


def save_comparison(strategy_profiles: dict[str, dict],
                    beta_matrix: dict,
                    output_dir: str,
                    accuracy: dict[str, dict] | None = None):
    """Save all comparison artifacts to output_dir."""
    os.makedirs(output_dir, exist_ok=True)

    # Save raw profiles
    serializable = {}
    for strategy, profile in strategy_profiles.items():
        serializable[strategy] = {
            feat: {"mean": v["mean"], "se": v["se"]}
            for feat, v in profile.items()
        }

    profiles_path = os.path.join(output_dir, "strategy_profiles.json")
    Path(profiles_path).write_text(json.dumps(serializable, indent=2))
    print(f"  Saved profiles: {profiles_path}")

    # Save beta diversity
    beta_serializable = {
        f"{a} vs {b}": v for (a, b), v in beta_matrix.items()
    }
    beta_path = os.path.join(output_dir, "beta_diversity.json")
    Path(beta_path).write_text(json.dumps(beta_serializable, indent=2))
    print(f"  Saved beta diversity: {beta_path}")

    # Radar chart
    plot_radar(strategy_profiles, os.path.join(output_dir, "radar_chart.png"),
               accuracy=accuracy)

    # Per-feature bar charts
    plot_feature_bars(strategy_profiles, output_dir)

    # Summary table
    table = print_summary_table(strategy_profiles, accuracy=accuracy)
    table_path = os.path.join(output_dir, "summary_table.txt")
    Path(table_path).write_text(table)
