"""
TDA pipeline and feature extraction (Sections 8-9 of spec).

Features 1-18 per (strategy, clue) pair:
  1-7:   Core TDA features (Vietoris-Rips persistence)
  8-12:  Tree-structural features
  13-16: Per-facet entropy
  17-18: Complementary distance features
"""

from __future__ import annotations

import math
from collections import Counter

import numpy as np

from .prompts import FACET_KEYS
from .ideas import (
    build_trees,
    extract_leaf_ideas,
    deduplicate_ideas,
    idea_to_frozenset,
    compute_distance_matrix,
    tree_structural_features,
    _make_hashable,
)


def shannon_entropy(values) -> float:
    """Compute Shannon entropy of a sequence of values."""
    if len(values) == 0:
        return 0.0
    counts = Counter(values)
    total = sum(counts.values())
    return -sum(
        (c / total) * math.log2(c / total)
        for c in counts.values()
        if c > 0
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Core TDA features (Section 9.1)
# ═══════════════════════════════════════════════════════════════════════════════

def tda_features(D: np.ndarray) -> dict:
    """Compute features 1-7 from the distance matrix using Ripser.

    Returns dict with keys: n_ideas, h0_mean_death, h0_cv, h0_entropy,
    h1_total_pers, h1_max_pers, h1_count.
    """
    n = D.shape[0]
    features = {
        "n_ideas": n,
        "h0_mean_death": 0.0,
        "h0_cv": 0.0,
        "h0_entropy": 0.0,
        "h1_total_pers": 0.0,
        "h1_max_pers": 0.0,
        "h1_count": 0,
    }

    if n < 2:
        return features

    try:
        from ripser import ripser
    except ImportError:
        print("  Warning: ripser not installed, TDA features will be zeros")
        return features

    result = ripser(D, maxdim=1, distance_matrix=True)
    h0_bars = result["dgms"][0]
    h1_bars = result["dgms"][1]

    # H0: connected components (exclude the infinite bar)
    h0_deaths = h0_bars[h0_bars[:, 1] < np.inf, 1]

    if len(h0_deaths) > 0:
        mean_death = h0_deaths.mean()
        std_death = h0_deaths.std()
        features["h0_mean_death"] = float(mean_death)
        features["h0_cv"] = float(std_death / (mean_death + 1e-12))
        features["h0_entropy"] = float(shannon_entropy(
            np.round(h0_deaths, 4).tolist()
        ))

    # H1: loops
    if len(h1_bars) > 0:
        persistences = h1_bars[:, 1] - h1_bars[:, 0]
        features["h1_total_pers"] = float(persistences.sum())
        features["h1_max_pers"] = float(persistences.max())

        # Count H1 bars above median H0 death
        if len(h0_deaths) > 0:
            threshold = float(np.median(h0_deaths))
            features["h1_count"] = int((persistences > threshold).sum())
        else:
            features["h1_count"] = len(h1_bars)

    return features


# ═══════════════════════════════════════════════════════════════════════════════
# Per-facet entropy (Section 9.3)
# ═══════════════════════════════════════════════════════════════════════════════

def facet_entropies(leaves: list[dict]) -> dict:
    """Compute per-facet entropy.

    Returns dict with one ``<facet>_entropy`` key per facet defined in
    ``prompts.FACET_KEYS`` (15 keys for Rosetta).
    """
    result = {}
    for facet in FACET_KEYS:
        values = [
            _make_hashable(leaf.get(facet))
            for leaf in leaves
            if leaf.get(facet) is not None
        ]
        result[f"{facet}_entropy"] = shannon_entropy(values)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Complementary features (Section 9.4)
# ═══════════════════════════════════════════════════════════════════════════════

def complementary_features(D: np.ndarray) -> dict:
    """Compute features 17-18: mean nearest-neighbor distance and diameter."""
    n = D.shape[0]
    if n < 2:
        return {"mean_nn_jaccard": 0.0, "diameter": 0.0}

    # Mean nearest-neighbor distance
    nn_dists = []
    for i in range(n):
        row = D[i]
        # Exclude self (distance 0)
        others = np.concatenate([row[:i], row[i + 1:]])
        if len(others) > 0:
            nn_dists.append(others.min())

    mean_nn = float(np.mean(nn_dists)) if nn_dists else 0.0
    diameter = float(D.max())

    return {"mean_nn_jaccard": mean_nn, "diameter": diameter}


# ═══════════════════════════════════════════════════════════════════════════════
# Full feature extraction for one (strategy, clue) pair
# ═══════════════════════════════════════════════════════════════════════════════

def extract_features(coded_result: dict) -> dict:
    """Extract all 18 features from a single coded result.

    Input: dict with 'trace', 'clue_text', etc. (output of code_one).
    Returns: dict of 18 named features.
    """
    trace = coded_result.get("trace", [])

    # Build trees and extract leaves
    trees = build_trees(trace)
    raw_leaves = extract_leaf_ideas(trees)
    leaves = deduplicate_ideas(raw_leaves)

    # Convert to frozensets
    idea_sets = [idea_to_frozenset(leaf) for leaf in leaves]
    # Remove empty frozensets (all-null facets)
    idea_sets = [s for s in idea_sets if s]

    features = {}

    # Distance matrix
    if len(idea_sets) >= 2:
        D = compute_distance_matrix(idea_sets)
    else:
        D = np.zeros((max(len(idea_sets), 1), max(len(idea_sets), 1)))

    # Features 1-7: TDA
    features.update(tda_features(D))
    features["n_ideas"] = len(idea_sets)

    # Features 8-12: Tree structure
    features.update(tree_structural_features(trees, leaves))

    # Features 13-16: Per-facet entropy
    features.update(facet_entropies(leaves))

    # Features 17-18: Complementary
    features.update(complementary_features(D))

    return features


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-solver: pool ideas across strategies for a clue
# ═══════════════════════════════════════════════════════════════════════════════

def extract_features_pooled(coded_results_by_strategy: dict[str, dict],
                            ) -> dict[str, dict]:
    """Extract features for each strategy, pooling all ideas across
    strategies into a shared distance matrix for a single clue.

    Input: {strategy_name: coded_result} for one clue.
    Returns: {strategy_name: features_dict}.
    """
    # Collect all ideas with strategy labels
    all_idea_sets = []
    strategy_labels = []

    for strategy, result in coded_results_by_strategy.items():
        trace = result.get("trace", [])
        trees = build_trees(trace)
        raw_leaves = extract_leaf_ideas(trees)
        leaves = deduplicate_ideas(raw_leaves)

        for leaf in leaves:
            fs = idea_to_frozenset(leaf)
            if fs:
                all_idea_sets.append(fs)
                strategy_labels.append(strategy)

    if len(all_idea_sets) < 2:
        # Not enough ideas to compute meaningful features
        return {s: extract_features(r)
                for s, r in coded_results_by_strategy.items()}

    # Shared distance matrix
    D_full = compute_distance_matrix(all_idea_sets)

    # Per-strategy feature extraction using subset of shared matrix
    results = {}
    for strategy, result in coded_results_by_strategy.items():
        # Get indices for this strategy
        indices = [i for i, s in enumerate(strategy_labels) if s == strategy]

        if len(indices) < 2:
            results[strategy] = extract_features(result)
            continue

        D_sub = D_full[np.ix_(indices, indices)]

        trace = result.get("trace", [])
        trees = build_trees(trace)
        raw_leaves = extract_leaf_ideas(trees)
        leaves = deduplicate_ideas(raw_leaves)

        features = {}
        features.update(tda_features(D_sub))
        features["n_ideas"] = len(indices)
        features.update(tree_structural_features(trees, leaves))
        features.update(facet_entropies(leaves))
        features.update(complementary_features(D_sub))

        results[strategy] = features

    return results


def beta_diversity(strategy_a_sets: list[frozenset],
                   strategy_b_sets: list[frozenset]) -> float:
    """Jaccard overlap between two strategy idea sets (Section 12.4)."""
    a = set(strategy_a_sets)
    b = set(strategy_b_sets)
    if not a and not b:
        return 0.0
    return 1.0 - len(a & b) / len(a | b)
