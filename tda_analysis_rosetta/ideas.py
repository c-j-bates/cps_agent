"""
Idea extraction and tree building from coded traces.

Implements Sections 2, 6, and 7 of the TDA Creativity Spec v3 (adapted
for Rosetta facets — see prompts.FACET_KEYS):
  - Build idea trees from trace operations
  - Extract leaf ideas as facet-dict tuples
  - Convert to canonical frozensets
  - Compute Jaccard distances
"""

from __future__ import annotations

import numpy as np

from .prompts import FACET_KEYS


# ═══════════════════════════════════════════════════════════════════════════════
# Tree building
# ═══════════════════════════════════════════════════════════════════════════════

class IdeaNode:
    """A node in an idea tree, representing one commitment."""

    __slots__ = ("facets", "children", "depth")

    def __init__(self, facets: dict, depth: int = 0):
        self.facets = facets  # subset of prompts.FACET_KEYS → value
        self.children = []
        self.depth = depth

    def add_child(self, facets: dict) -> "IdeaNode":
        child = IdeaNode(facets, depth=self.depth + 1)
        self.children.append(child)
        return child

    @property
    def is_leaf(self) -> bool:
        return len(self.children) == 0


class IdeaTree:
    """A tree of ideas for a single clue from a single solver."""

    def __init__(self, tree_id: str):
        self.tree_id = tree_id
        self.roots = []  # list of IdeaNode

    def add_at_path(self, parent_path: list[int], facets: dict) -> IdeaNode:
        """Add a node at the given path. [] = new root child."""
        if not parent_path:
            node = IdeaNode(facets, depth=0)
            self.roots.append(node)
            return node

        # Ensure root exists (may have been removed by dedup)
        while len(self.roots) <= parent_path[0]:
            self.roots.append(IdeaNode({}, depth=0))

        # Navigate to parent
        current = self.roots[parent_path[0]]
        for idx in parent_path[1:]:
            while len(current.children) <= idx:
                current.add_child({})
            current = current.children[idx]

        return current.add_child(facets)


def build_trees(trace: list) -> dict[str, IdeaTree]:
    """Build idea trees from a parsed trace.

    Returns {tree_id: IdeaTree}.
    """
    trees = {}
    for entry in trace:
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            continue
        tree_id = str(entry[0])
        parent_path = entry[1] if isinstance(entry[1], list) else []
        facets = entry[2] if isinstance(entry[2], dict) else {}

        if tree_id not in trees:
            trees[tree_id] = IdeaTree(tree_id)

        trees[tree_id].add_at_path(parent_path, facets)

    return trees


# ═══════════════════════════════════════════════════════════════════════════════
# Leaf extraction (Section 6)
# ═══════════════════════════════════════════════════════════════════════════════

def _collect_leaves(node: IdeaNode, accumulated: dict) -> list[dict]:
    """Collect leaf facets, accumulating parent facets along the path."""
    # Merge: child facets override parent for non-None values
    merged = dict(accumulated)
    for key in FACET_KEYS:
        val = node.facets.get(key)
        if val is not None:
            merged[key] = val

    if node.is_leaf:
        return [merged]

    leaves = []
    for child in node.children:
        leaves.extend(_collect_leaves(child, merged))
    return leaves


def extract_leaf_ideas(trees: dict[str, IdeaTree]) -> list[dict]:
    """Extract all leaf ideas from trees as 4-facet dicts.

    Each leaf inherits non-null facets from its ancestors.
    """
    leaves = []
    for tree in trees.values():
        for root in tree.roots:
            leaves.extend(_collect_leaves(root, {}))
    return leaves


# ═══════════════════════════════════════════════════════════════════════════════
# Canonical frozensets (Section 6)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_hashable(val):
    """Make nested structures hashable for frozenset conversion."""
    if val is None:
        return None
    if isinstance(val, list):
        return tuple(_make_hashable(v) for v in val)
    if isinstance(val, dict):
        return tuple(sorted((k, _make_hashable(v)) for k, v in val.items()))
    return val


def idea_to_frozenset(idea: dict) -> frozenset:
    """Convert a faceted idea dict to a canonical frozenset.

    Null facets are excluded (two ideas leaving a facet null do NOT
    get credit for agreeing). The set of facet keys considered is
    ``prompts.FACET_KEYS`` (15 entries for Rosetta).
    """
    parts = []
    for key in FACET_KEYS:
        val = idea.get(key)
        if val is not None:
            parts.append((key, _make_hashable(val)))
    return frozenset(parts)


def deduplicate_ideas(ideas: list[dict]) -> list[dict]:
    """Remove duplicate ideas (same canonical frozenset)."""
    seen = set()
    unique = []
    for idea in ideas:
        fs = idea_to_frozenset(idea)
        if fs and fs not in seen:
            seen.add(fs)
            unique.append(idea)
    return unique


# ═══════════════════════════════════════════════════════════════════════════════
# Jaccard distance (Section 7)
# ═══════════════════════════════════════════════════════════════════════════════

def jaccard_distance(a: frozenset, b: frozenset) -> float:
    """Compute Jaccard distance between two idea frozensets."""
    if not a and not b:
        return 0.0
    return 1.0 - len(a & b) / len(a | b)


def compute_distance_matrix(idea_sets: list[frozenset]) -> np.ndarray:
    """Compute pairwise Jaccard distance matrix.

    Returns an (n, n) symmetric matrix.
    """
    n = len(idea_sets)
    D = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            d = jaccard_distance(idea_sets[i], idea_sets[j])
            D[i, j] = d
            D[j, i] = d
    return D


# ═══════════════════════════════════════════════════════════════════════════════
# Tree-structural features (for Section 9.2)
# ═══════════════════════════════════════════════════════════════════════════════

def tree_structural_features(trees: dict[str, IdeaTree],
                             leaves: list[dict]) -> dict:
    """Compute tree-structural features (features 8-12 from spec)."""
    n_root_branches = sum(len(t.roots) for t in trees.values())

    # Collect all fanouts and depths
    fanouts = []
    depths = []

    def _visit(node: IdeaNode):
        fanouts.append(len(node.children))
        if node.is_leaf:
            depths.append(node.depth)
        for child in node.children:
            _visit(child)

    for tree in trees.values():
        for root in tree.roots:
            _visit(root)

    max_local_fanout = max(fanouts) if fanouts else 0
    mean_depth = sum(depths) / len(depths) if depths else 0.0

    # Completion ratio: fraction of leaves with EVERY facet non-null.
    # For Rosetta that means a leaf has committed to all 14 facets —
    # rare, since most ideas touch only a few. Solver outputs almost
    # never describe the entire conlang in a single trace entry, so
    # this metric will hug 0 for Rosetta. Kept here for parity with the
    # MC feature schema and because aggregation downstream expects it.
    n_complete = sum(
        1 for leaf in leaves
        if all(leaf.get(k) is not None for k in FACET_KEYS)
    )
    completion_ratio = n_complete / len(leaves) if leaves else 0.0

    # Subtree overlap: fraction of ideas appearing under multiple parents
    # (approximated as 0 for now — requires more complex tracking)
    subtree_overlap = 0.0

    return {
        "n_root_branches": n_root_branches,
        "max_local_fanout": max_local_fanout,
        "completion_ratio": completion_ratio,
        "mean_depth": mean_depth,
        "subtree_overlap": subtree_overlap,
    }
