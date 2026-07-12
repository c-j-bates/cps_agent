"""
Visualize idea traces as incrementally-built tree diagrams + animated GIFs.

Each frame adds one trace entry (node) to the tree. Nodes are rendered as
vertically-stacked coloured segments — one per new facet — drawn as exact
Rectangle patches so spacing is pixel-perfect.
"""

from __future__ import annotations

import json
import os
import textwrap
from collections import defaultdict
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════════
# Per-facet colours
# ═══════════════════════════════════════════════════════════════════════════════

_FACET_COLORS = {
    "parse":     "#6A1B9A",   # purple
    "mechanism": "#E65100",   # orange
    "execution": "#0277BD",   # teal-blue
    "output":    "#2E7D32",   # green
}

# ═══════════════════════════════════════════════════════════════════════════════
# Layout & rendering constants
#
# All spatial values are in "data units". We size the figure so that
# 1 data unit ≈ 1 inch, giving consistent text sizing at fontsize 7.
# ═══════════════════════════════════════════════════════════════════════════════

_FONT_SIZE = 6
_CHAR_W = 0.065       # measured: monospace fs6, 1 du/inch, 150 dpi
_LINE_H = 0.12        # measured: monospace fs6 line height in data-units
_SEG_PAD_Y = 0.025    # vertical pad inside each segment (above & below text)
_SEG_PAD_X = 0.08     # horizontal pad inside each segment
_WRAP_WIDTH = 22       # characters before wrapping
_EDGE_GAP = 0.15      # data-units of visible gap between parent and child


# ═══════════════════════════════════════════════════════════════════════════════
# Node label formatting
# ═══════════════════════════════════════════════════════════════════════════════

def _fmt_parse(p) -> str:
    if p is None:
        return ""
    if isinstance(p, dict):
        pos = p.get("position", "?")
        text = p.get("text", "")
        if pos == "whole":
            return "def: [whole clue]"
        return f'def: "{text}" ({pos})'
    return str(p)


def _fmt_mechanism(m) -> str:
    if m is None:
        return ""
    if isinstance(m, list):
        return "mech: " + " → ".join(str(x) for x in m)
    return f"mech: {m}"


def _fmt_execution(ex) -> str:
    if ex is None:
        return ""
    if not isinstance(ex, list):
        return str(ex)
    parts = []
    for step in ex:
        if not isinstance(step, dict):
            parts.append(str(step))
            continue
        indicator = step.get("indicator", {})
        ind_text = indicator.get("text", "") if isinstance(indicator, dict) else str(indicator)
        result = step.get("result", "?")
        fodder = step.get("fodder", step.get("inner", step.get("outer", {})))
        fod_text = fodder.get("text", "") if isinstance(fodder, dict) else str(fodder)
        if ind_text and fod_text:
            parts.append(f'"{ind_text}"({fod_text})→{result}')
        elif fod_text:
            parts.append(f'{fod_text}→{result}')
        elif result:
            parts.append(f'→{result}')
    return "exec: " + "; ".join(parts)


def _fmt_output(o) -> str:
    if o is None:
        return ""
    return f"ans: {o}"


_FACET_FMT = [
    ("parse",     _fmt_parse),
    ("mechanism", _fmt_mechanism),
    ("execution", _fmt_execution),
    ("output",    _fmt_output),
]


def _compute_segments(facets: dict,
                      parent_facets: dict | None = None) -> list[tuple[str, str]]:
    """Return [(facet_key, display_text), ...] for new facets only."""
    segments = []
    for key, fmt in _FACET_FMT:
        val = facets.get(key)
        if val is None:
            continue
        if parent_facets is not None and parent_facets.get(key) == val:
            continue
        text = fmt(val)
        if text:
            wrapped = textwrap.fill(text, width=_WRAP_WIDTH)
            segments.append((key, wrapped))
    return segments


def _seg_height(text: str) -> float:
    """Height of one segment in data units."""
    return len(text.split("\n")) * _LINE_H + 2 * _SEG_PAD_Y


# ═══════════════════════════════════════════════════════════════════════════════
# Tree layout
# ═══════════════════════════════════════════════════════════════════════════════

class LayoutNode:
    """A node positioned for rendering."""
    __slots__ = ("tree_id", "path", "facets", "segments", "x", "y",
                 "children", "is_new", "est_width", "est_height")

    def __init__(self, tree_id: str, path: list, facets: dict,
                 parent_facets: dict | None = None):
        self.tree_id = tree_id
        self.path = path
        self.facets = facets
        self.segments = _compute_segments(facets, parent_facets)
        self.x = 0.0
        self.y = 0.0
        self.children: list[LayoutNode] = []
        self.is_new = False

        if self.segments:
            max_line_len = 0
            for _, text in self.segments:
                for line in text.split("\n"):
                    max_line_len = max(max_line_len, len(line))
            self.est_width = max_line_len * _CHAR_W + 2 * _SEG_PAD_X
            self.est_height = sum(_seg_height(t) for _, t in self.segments)
        else:
            self.est_width = 7 * _CHAR_W + 2 * _SEG_PAD_X
            self.est_height = _LINE_H + 2 * _SEG_PAD_Y


def _build_layout_trees(trace_entries: list) -> dict[str, list[LayoutNode]]:
    """Build layout trees from trace entries.

    Assumes trace has been through clean_trace() so paths are valid.
    """
    trees: dict[str, list[LayoutNode]] = defaultdict(list)
    node_index: dict[tuple[str, tuple], LayoutNode] = {}

    for entry in trace_entries:
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            continue
        tree_id = str(entry[0])
        path = list(entry[1]) if isinstance(entry[1], list) else []
        facets = entry[2] if isinstance(entry[2], dict) else {}

        parent_facets = None
        if path:
            pkey = (tree_id, tuple(path[:-1]))
            pnode = node_index.get(pkey)
            if pnode is not None:
                parent_facets = pnode.facets

        node = LayoutNode(tree_id, path, facets, parent_facets)
        node_index[(tree_id, tuple(path))] = node

        if not path:
            trees[tree_id].append(node)
        else:
            pkey = (tree_id, tuple(path[:-1]))
            if pkey in node_index:
                node_index[pkey].children.append(node)
            else:
                trees[tree_id].append(node)

    return dict(trees)


def _collect_all_nodes(roots_by_tree: dict[str, list[LayoutNode]]) -> list[LayoutNode]:
    result: list[LayoutNode] = []
    def _visit(n: LayoutNode):
        result.append(n)
        for c in n.children:
            _visit(c)
    for tid in sorted(roots_by_tree.keys()):
        for r in roots_by_tree[tid]:
            _visit(r)
    return result


def _assign_positions(roots_by_tree: dict[str, list[LayoutNode]]):
    """Assign (x, y) with tight dynamic spacing.

    y_step = max_node_height + _EDGE_GAP, so the visible gap between
    the bottom of the tallest parent and top of the tallest child
    is exactly _EDGE_GAP data-units.
    """
    all_nodes = _collect_all_nodes(roots_by_tree)
    if not all_nodes:
        return

    max_h = max(n.est_height for n in all_nodes)
    max_w = max(n.est_width for n in all_nodes)

    y_step = max_h + _EDGE_GAP
    x_sib_gap = max_w + 0.08
    x_tree_gap = max_w * 0.2 + 0.10

    x_cursor = 0.0
    for tid in sorted(roots_by_tree.keys()):
        for root in roots_by_tree[tid]:
            w = _assign_x(root, x_cursor, x_sib_gap)
            _assign_y(root, 0.0, y_step)
            x_cursor += w + x_tree_gap


def _assign_x(node: LayoutNode, x_start: float, gap: float) -> float:
    if not node.children:
        node.x = x_start
        return gap
    total = 0.0
    for child in node.children:
        total += _assign_x(child, x_start + total, gap)
    node.x = (node.children[0].x + node.children[-1].x) / 2
    return total


def _assign_y(node: LayoutNode, y: float, y_step: float):
    node.y = y
    for child in node.children:
        _assign_y(child, y + y_step, y_step)


# ═══════════════════════════════════════════════════════════════════════════════
# Rendering
# ═══════════════════════════════════════════════════════════════════════════════

def _draw_node(ax, node: LayoutNode):
    """Draw a node as tightly-stacked coloured Rectangle patches + text."""
    from matplotlib.patches import Rectangle

    segs = node.segments
    w = node.est_width
    h_total = node.est_height

    if not segs:
        rect = Rectangle((node.x - w / 2, node.y - h_total / 2), w, h_total,
                          facecolor="#9E9E9E", edgecolor="#777",
                          linewidth=0.5, alpha=0.7, zorder=2)
        ax.add_patch(rect)
        ax.text(node.x, node.y, "(empty)", ha="center", va="center",
                fontsize=_FONT_SIZE, fontfamily="monospace", color="white",
                zorder=3)
        return

    # Highlight border for newest node
    if node.is_new:
        m = 0.025
        rect = Rectangle((node.x - w / 2 - m, node.y - h_total / 2 - m),
                          w + 2 * m, h_total + 2 * m,
                          facecolor="none", edgecolor="#FFC107",
                          linewidth=2.5, zorder=4, clip_on=False)
        ax.add_patch(rect)

    # Draw each segment as a filled rectangle + overlaid text
    y_top = node.y - h_total / 2  # top of first segment (smallest y = top)

    for key, text in segs:
        h = _seg_height(text)
        color = _FACET_COLORS.get(key, "#9E9E9E")

        rect = Rectangle((node.x - w / 2, y_top), w, h,
                          facecolor=color, edgecolor="white",
                          linewidth=0.3, alpha=0.92, zorder=2)
        ax.add_patch(rect)

        ax.text(node.x, y_top + h / 2, text,
                ha="center", va="center",
                fontsize=_FONT_SIZE, fontfamily="monospace",
                color="white", zorder=3, clip_on=False)

        y_top += h


def render_frame(roots_by_tree: dict[str, list[LayoutNode]],
                 title: str, step: int, total_steps: int,
                 out_path: str, subtitle: str = ""):
    """Render one frame of the tree to a PNG file."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    all_nodes = _collect_all_nodes(roots_by_tree)
    if not all_nodes:
        return

    xs = [n.x for n in all_nodes]
    ys = [n.y for n in all_nodes]
    max_w = max(n.est_width for n in all_nodes)
    max_h = max(n.est_height for n in all_nodes)

    # Content bounds (nodes + tree labels)
    content_left  = min(xs) - max_w / 2
    content_right = max(xs) + max_w / 2
    content_top   = min(ys) - max_h / 2 - 0.20  # room for tree ID labels
    content_bot   = max(ys) + max_h / 2

    # Legend: just right of rightmost tree
    legend_gap = 0.20
    legend_line_h = 0.18
    legend_x = content_right + legend_gap
    legend_top = content_top

    # Title
    title_text = f"{title}  [step {step}/{total_steps}]"
    if subtitle:
        title_text += f"\n{subtitle}"
    title_h = 0.45

    # Figure bounds: driven by content + legend, NOT by title.
    # Title is placed with clip_on=False and bbox_inches='tight'
    # will expand the saved image to include it.
    margin = 0.10
    x_min = content_left - margin
    x_max = legend_x + 1.0 + margin   # 1.0 ≈ legend text width
    y_min = content_top - title_h - margin
    y_max = content_bot + margin

    fig_w = max(4, x_max - x_min)
    fig_h = max(2, y_max - y_min)

    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlim(x_min, x_max)
    ax.set_ylim(y_max, y_min)
    ax.set_aspect("equal")
    ax.axis("off")

    # Title: centered on content+legend span, above tree labels
    title_x = (content_left + legend_x + 1.0) / 2
    title_y = content_top - 0.08
    ax.text(title_x, title_y, title_text,
            ha="center", va="bottom",
            fontsize=9, fontweight="bold", zorder=5,
            clip_on=False)

    # Edges: parent bottom → child top
    for node in all_nodes:
        p_bot = node.y + node.est_height / 2
        for child in node.children:
            c_top = child.y - child.est_height / 2
            ax.plot([node.x, child.x], [p_bot, c_top],
                    color="#BDBDBD", linewidth=1.2, zorder=1)

    # Nodes
    for node in all_nodes:
        _draw_node(ax, node)

    # Tree ID labels
    for tid, roots in sorted(roots_by_tree.items()):
        if roots:
            rx = sum(r.x for r in roots) / len(roots)
            ry = min(r.y for r in roots) - max_h / 2 - 0.08
            ax.text(rx, ry, tid, ha="center", va="bottom",
                    fontsize=7, fontweight="bold", color="#555", zorder=3)

    # Legend: in data coords, just right of rightmost tree
    for i, (label, color) in enumerate(_FACET_COLORS.items()):
        ax.text(legend_x, legend_top + i * legend_line_h, f"● {label}",
                ha="left", va="top",
                fontsize=6, color=color, fontweight="bold", zorder=5)

    fig.savefig(out_path, dpi=150, bbox_inches="tight",
                pad_inches=0.08, facecolor="white", edgecolor="none")
    plt.close(fig)


# ═══════════════════════════════════════════════════════════════════════════════
# GIF generation
# ═══════════════════════════════════════════════════════════════════════════════

def visualize_trace(trace: list, title: str, output_dir: str,
                    subtitle: str = "") -> str | None:
    """Generate step-by-step PNG frames and a combined GIF for a trace."""
    from PIL import Image

    if not trace:
        return None

    frames_dir = os.path.join(output_dir, "frames")
    # Remove stale frames from prior runs with different step counts
    if os.path.isdir(frames_dir):
        for old in Path(frames_dir).glob("step_*.png"):
            old.unlink()
    os.makedirs(frames_dir, exist_ok=True)

    total_steps = len(trace)
    image_paths = []

    for step in range(1, total_steps + 1):
        entries_so_far = trace[:step]
        roots_by_tree = _build_layout_trees(entries_so_far)
        _assign_positions(roots_by_tree)

        newest = entries_so_far[-1]
        newest_key = (str(newest[0]),
                      tuple(newest[1]) if isinstance(newest[1], list) else ())
        for node in _collect_all_nodes(roots_by_tree):
            node.is_new = (node.tree_id == newest_key[0]
                           and tuple(node.path) == newest_key[1])

        img_path = os.path.join(frames_dir, f"step_{step:03d}.png")
        render_frame(roots_by_tree, title, step, total_steps, img_path,
                     subtitle=subtitle)
        image_paths.append(img_path)

    if not image_paths:
        return None

    frames = [Image.open(p) for p in image_paths]
    max_w = max(f.width for f in frames)
    max_h = max(f.height for f in frames)

    uniform = []
    for f in frames:
        if f.width != max_w or f.height != max_h:
            nf = Image.new("RGB", (max_w, max_h), (255, 255, 255))
            nf.paste(f, (0, 0))
            uniform.append(nf)
        else:
            uniform.append(f)

    gif_path = os.path.join(output_dir, "evolution.gif")
    durations = [1500] * len(uniform)
    durations[-1] = 4000
    uniform[0].save(gif_path, save_all=True, append_images=uniform[1:],
                    duration=durations, loop=0)
    print(f"  Visualization: {len(image_paths)} frames → {gif_path}")
    return gif_path


# ═══════════════════════════════════════════════════════════════════════════════
# Batch
# ═══════════════════════════════════════════════════════════════════════════════

def visualize_coded_results(coding_dir: str, vis_base_dir: str | None = None,
                            strategy: str = "", model: str = "",
                            experiment_name: str = ""):
    """Generate visualizations for all *_cleaned.json files in a coding dir."""
    coding_path = Path(coding_dir)
    if vis_base_dir is None:
        vis_base_dir = str(coding_path.parent / "visualizations")

    json_files = sorted(coding_path.glob("*_cleaned.json"))
    if not json_files:
        print(f"  No *_cleaned.json files in {coding_dir}")
        return

    parts = []
    if strategy:
        parts.append(f"strategy={strategy}")
    if model:
        parts.append(f"model={model}")
    if experiment_name and not (strategy and model):
        parts.append(experiment_name)
    subtitle = "  |  ".join(parts)

    for jf in json_files:
        data = json.loads(jf.read_text())
        trace = data.get("trace", [])
        if not trace:
            continue

        clue = data.get("clue_text", "")
        stem = jf.stem.replace("_cleaned", "")
        title = clue if len(clue) <= 60 else clue[:57] + "..."

        # Add solution info to title
        correct = data.get("correct_solution")
        solver_ans = data.get("solver_answer")
        if correct:
            if solver_ans and solver_ans.upper() == correct.upper():
                title += f"  [answer: {correct} \u2713]"
            elif solver_ans:
                title += f"  [answer: {correct}, solver: {solver_ans} \u2717]"
            else:
                title += f"  [answer: {correct}]"

        vis_dir = os.path.join(vis_base_dir, stem)
        print(f"  Visualizing {stem} ({len(trace)} steps)...")
        visualize_trace(trace, title, vis_dir, subtitle=subtitle)
