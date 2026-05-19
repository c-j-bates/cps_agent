"""
Idea Graph Analysis Pipeline

Stages (run individually or end-to-end):
  1. extract  – Parse .md experiment logs → LLM-output text + token counts
  2. analyze  – Call Opus 4.6 with the extraction prompt → raw analysis .txt
  3. clean    – Call Opus 4.6 with the dedup prompt → cleaned analysis .txt
  4. metrics  – Compute breadth-of-ideation metrics → metrics.json
  5. visualize – Render step-by-step graph images + GIF from cleaned analyses

Usage:
    # Full pipeline
    python idea_graph_analysis.py experiment_logs/minute-cryptic-par1

    # Re-clean existing raw analyses (no new LLM extraction calls)
    python idea_graph_analysis.py experiment_logs/minute-cryptic-par1 --clean-only

    # Recompute metrics from existing cleaned analyses
    python idea_graph_analysis.py experiment_logs/minute-cryptic-par1 --metrics-only

    # Re-visualize from existing cleaned analyses
    python idea_graph_analysis.py experiment_logs/minute-cryptic-par1 --visualize-only

    # Visualize a single file
    python idea_graph_analysis.py experiment_logs/minute-cryptic-par1 --visualize-only some_file_cleaned.txt
"""

import argparse
import os
import re
import glob
import json
import ast
from pathlib import Path

from llm_clients import AnthropicClient

# Canonical parent keys (and sub-keys) that always appear in visualizations,
# matching the schema in idea_graph_analysis_prompt.txt.
ALL_PARENTS = ["definition", "indicators", "indicator_sequences", "answer_guesses", "operations"]
INDICATOR_SUBKEYS = ["words", "types", "subtypes"]

SCRIPT_DIR = os.path.dirname(__file__)


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 1: Parsing
# ═══════════════════════════════════════════════════════════════════════════════

def parse_calls(md_text: str) -> list[dict]:
    """Parse all LLM calls from an experiment log .md file.

    Returns a list of dicts with keys: number, action, channel, prompt, response.
    """
    call_pattern = re.compile(
        r"^### Call (\d+): (.+?) \((.+?)\)\s*$", re.MULTILINE
    )
    matches = list(call_pattern.finditer(md_text))
    calls = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        section = md_text[start:end]

        prompt = _extract_block(section, "Prompt")
        response = _extract_block(section, "Response")

        calls.append({
            "number": int(m.group(1)),
            "action": m.group(2),
            "channel": m.group(3),
            "prompt": prompt,
            "response": response,
        })
    return calls


def _extract_block(section: str, label: str) -> str:
    """Extract the code-fenced block after **Label**: in a call section."""
    pattern = re.compile(
        rf"\*\*{label}\*\*:\s*\n\n````\n(.*?)````",
        re.DOTALL,
    )
    m = pattern.search(section)
    if m:
        return m.group(1).rstrip("\n")
    # Fallback: triple backticks
    pattern = re.compile(
        rf"\*\*{label}\*\*:\s*\n\n```\n(.*?)```",
        re.DOTALL,
    )
    m = pattern.search(section)
    return m.group(1).rstrip("\n") if m else ""


def extract_llm_outputs(md_path: str) -> str:
    """Extract the last main-channel prompt+response and subsequent side-channel chain."""
    md_text = Path(md_path).read_text()
    calls = parse_calls(md_text)
    if not calls:
        return ""

    last_main_idx = None
    for i, c in enumerate(calls):
        if c["channel"] == "main_channel":
            last_main_idx = i

    if last_main_idx is None:
        return ""

    last_main = calls[last_main_idx]

    parts = []
    parts.append(f"{last_main['prompt']}")
    parts.append(f"\n\n{last_main['response']}")

    for c in calls[last_main_idx + 1:]:
        parts.append(f"\n\n{c['prompt']}")
        parts.append(f"\n\n{c['response']}")

    return "\n".join(parts)


def extract_token_counts(md_path: str) -> dict:
    """Extract token counts from the Token Summary table in an .md file.

    Returns dict with keys: total_input_tokens, total_output_tokens, total_tokens.
    """
    md_text = Path(md_path).read_text()
    counts = {}
    for key, pattern in [
        ("total_input_tokens",  r"\|\s*Total input tokens\s*\|\s*([\d,]+)\s*\|"),
        ("total_output_tokens", r"\|\s*Total output tokens\s*\|\s*([\d,]+)\s*\|"),
        ("total_tokens",        r"\|\s*Total tokens\s*\|\s*([\d,]+)\s*\|"),
    ]:
        m = re.search(pattern, md_text)
        counts[key] = int(m.group(1).replace(",", "")) if m else 0
    return counts


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 2 & 3: LLM Calling (analyze + clean)
# ═══════════════════════════════════════════════════════════════════════════════

def _load_template(filename: str) -> str:
    return Path(os.path.join(SCRIPT_DIR, filename)).read_text()


def _make_client() -> AnthropicClient:
    return AnthropicClient(model="claude-opus-4-6", temperature=0.7)


# ── Shared single-problem entry point ────────────────────────────────────────

def analyze_one(md_path: str, cache_dir: str,
                client=None,
                analysis_template: str | None = None,
                dedup_template: str | None = None) -> dict | None:
    """Analyze a single .md file, caching artifacts in cache_dir.

    Skips LLM calls if <stem>_cleaned.txt already exists in cache_dir.
    Returns a metrics dict (with 'tokens' key), or None on failure.
    """
    stem = Path(md_path).stem
    os.makedirs(cache_dir, exist_ok=True)

    clean_path = os.path.join(cache_dir, f"{stem}_cleaned.txt")
    raw_path = os.path.join(cache_dir, f"{stem}_analysis.txt")

    tokens = extract_token_counts(md_path)

    # Cache hit — skip LLM calls but still regenerate visualizations
    if os.path.isfile(clean_path):
        print(f"  [{stem}] cached, skipping LLM calls")
        raw_response = Path(raw_path).read_text() if os.path.isfile(raw_path) else None
        cleaned = Path(clean_path).read_text()
    else:
        # Need to run LLM calls
        llm_outputs = extract_llm_outputs(md_path)
        if not llm_outputs:
            print(f"  [{stem}] skipping (no main-channel calls)")
            return None

        if analysis_template is None:
            analysis_template = _load_template("idea_graph_analysis_prompt.txt")
        if dedup_template is None:
            dedup_template = _load_template("idea_graph_dedup_prompt.txt")
        if client is None:
            client = _make_client()

        # Stage 2: Analyze
        analysis_prompt = analysis_template.replace("{llm_outputs}", llm_outputs)
        print(f"  [{stem}] analyzing...")
        raw_response = str(client(analysis_prompt))
        Path(raw_path).write_text(raw_response)

        # Stage 3: Clean
        full_raw = f"PROMPT:\n{analysis_prompt}\n\nRESPONSE:\n{raw_response}"
        clean_prompt = dedup_template.replace("{raw_analysis}", full_raw)
        print(f"  [{stem}] cleaning...")
        cleaned = str(client(clean_prompt))
        Path(clean_path).write_text(cleaned)

        # Diff
        diff_path = os.path.join(cache_dir, f"{stem}_diff.txt")
        _save_diff(raw_response, cleaned, diff_path)

    # Always regenerate visualizations (cheap, and picks up rendering fixes)
    if raw_response:
        visualize_idea_graph(raw_response, f"{stem}_raw", cache_dir)
    visualize_idea_graph(cleaned, stem, cache_dir)

    metrics = compute_metrics_for_text(cleaned)
    metrics["tokens"] = tokens
    return metrics


def find_mds_for_problem(experiment_log_dir: str, problem_id: int,
                         max_epochs: int | None = None) -> list[str]:
    """Find .md files for a problem ID in an experiment log directory.

    Returns up to max_epochs paths, sorted chronologically.
    """
    exp_dir = Path(experiment_log_dir)
    if not exp_dir.is_dir():
        return []
    matches = []
    for subdir in sorted(exp_dir.iterdir()):
        if not subdir.is_dir():
            continue
        parts = subdir.name.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) == problem_id:
            md_file = subdir / f"{subdir.name}.md"
            if md_file.is_file():
                matches.append(str(md_file))
    if max_epochs is not None:
        matches = matches[:max_epochs]
    return matches


def cache_dir_for(experiment_log_dir: str) -> str:
    """Return the shared analysis cache directory for an experiment log dir."""
    return os.path.join(experiment_log_dir, "idea_graph_analysis")


# ── Batch pipeline (used by CLI) ─────────────────────────────────────────────

def run_analysis(experiment_dir: str, output_dir: str):
    """Full pipeline: extract → analyze → clean → metrics → visualize."""
    experiment_dir = os.path.abspath(experiment_dir)
    os.makedirs(output_dir, exist_ok=True)

    analysis_template = _load_template("idea_graph_analysis_prompt.txt")
    dedup_template = _load_template("idea_graph_dedup_prompt.txt")

    md_files = _find_md_files(experiment_dir)
    if not md_files:
        return

    client = _make_client()
    results = {}

    for md_path in md_files:
        rel = os.path.relpath(md_path, experiment_dir)
        stem = Path(md_path).stem
        print(f"\nProcessing: {rel}")

        llm_outputs = extract_llm_outputs(md_path)
        if not llm_outputs:
            print(f"  Skipping (no main-channel calls found)")
            continue

        tokens = extract_token_counts(md_path)

        # Stage 2: Analyze
        analysis_prompt = analysis_template.replace("{llm_outputs}", llm_outputs)
        print(f"  Calling Opus 4.6 (analyze)...")
        raw_response = str(client(analysis_prompt))
        raw_path = os.path.join(output_dir, f"{stem}_analysis.txt")
        Path(raw_path).write_text(raw_response)
        print(f"  Saved raw: {raw_path}")

        # Stage 3: Clean — include the original prompt + response so the
        # dedup model has full context about what was being analyzed.
        full_raw = f"PROMPT:\n{analysis_prompt}\n\nRESPONSE:\n{raw_response}"
        clean_prompt = dedup_template.replace("{raw_analysis}", full_raw)
        print(f"  Calling Opus 4.6 (clean)...")
        cleaned_response = str(client(clean_prompt))
        clean_path = os.path.join(output_dir, f"{stem}_cleaned.txt")
        Path(clean_path).write_text(cleaned_response)
        print(f"  Saved cleaned: {clean_path}")

        diff_path = os.path.join(output_dir, f"{stem}_diff.txt")
        _save_diff(raw_response, cleaned_response, diff_path)
        print(f"  Saved diff: {diff_path}")

        results[stem] = {
            "md_path": md_path,
            "analysis_path": raw_path,
            "cleaned_path": clean_path,
            "tokens": tokens,
        }

    # Save index
    _save_index(output_dir, results)

    # Stage 4: Metrics
    print("\n── Computing metrics ──")
    compute_and_save_metrics(output_dir)

    # Stage 5: Visualize (from cleaned files)
    print("\n── Generating visualizations ──")
    visualize_all_in_dir(output_dir)


def run_clean_only(output_dir: str):
    """Re-clean existing raw analyses without re-running extraction."""
    analysis_template = _load_template("idea_graph_analysis_prompt.txt")
    dedup_template = _load_template("idea_graph_dedup_prompt.txt")
    raw_files = sorted(glob.glob(os.path.join(output_dir, "*_analysis.txt")))
    if not raw_files:
        print(f"No *_analysis.txt files found in {output_dir}")
        return

    # Load index to reconstruct the original analysis prompts
    index_path = os.path.join(output_dir, "index.json")
    index = {}
    if os.path.isfile(index_path):
        index = json.loads(Path(index_path).read_text())

    client = _make_client()
    print(f"Cleaning {len(raw_files)} raw analyses in {output_dir}")

    for raw_path in raw_files:
        stem = Path(raw_path).stem.replace("_analysis", "")
        raw_response = Path(raw_path).read_text()

        # Reconstruct the filled analysis prompt from the source .md
        analysis_prompt = ""
        md_path = index.get(stem, {}).get("md_path")
        if not md_path or not os.path.isfile(md_path):
            # Fallback: the stem is the subdir and filename by convention
            # e.g. output_dir = .../experiment_dir/idea_graph_analysis/
            #      experiment_dir = parent of output_dir
            experiment_dir = os.path.dirname(output_dir)
            candidate = os.path.join(experiment_dir, stem, f"{stem}.md")
            if os.path.isfile(candidate):
                md_path = candidate
        if md_path and os.path.isfile(md_path):
            llm_outputs = extract_llm_outputs(md_path)
            if llm_outputs:
                analysis_prompt = analysis_template.replace("{llm_outputs}", llm_outputs)

        if analysis_prompt:
            full_raw = f"PROMPT:\n{analysis_prompt}\n\nRESPONSE:\n{raw_response}"
        else:
            print(f"    Warning: could not reconstruct analysis prompt for {stem}")
            full_raw = raw_response

        clean_prompt = dedup_template.replace("{raw_analysis}", full_raw)
        print(f"  Cleaning {stem}...")
        cleaned_response = str(client(clean_prompt))
        clean_path = os.path.join(output_dir, f"{stem}_cleaned.txt")
        Path(clean_path).write_text(cleaned_response)
        print(f"    Saved: {clean_path}")

        diff_path = os.path.join(output_dir, f"{stem}_diff.txt")
        _save_diff(raw_response, cleaned_response, diff_path)
        print(f"    Saved diff: {diff_path}")

    # Recompute metrics
    print("\n── Recomputing metrics ──")
    compute_and_save_metrics(output_dir)


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 4: Metrics
# ═══════════════════════════════════════════════════════════════════════════════

def parse_graph_updates(response_text: str) -> list[dict]:
    """Parse sequential graph update statements from an analysis response.

    Handles both single-line and multi-line .append(...) calls (e.g. dicts
    that span several lines in the operations category).

    Returns a list of dicts: {key_phrase, path, value}.
    """
    updates = []

    code_blocks = re.findall(r"```python\n(.*?)```", response_text, re.DOTALL)
    full_code = "\n".join(code_blocks)

    lines = full_code.splitlines()
    current_key_phrase = ""
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Track key-phrase comments
        key_phrase_match = re.match(r"^#\s*Key phrase:\s*(.+)", line)
        if key_phrase_match:
            current_key_phrase = key_phrase_match.group(1).strip()
            i += 1
            continue

        # Match the start of a graph[...].append( statement
        append_match = re.match(
            r"graph\[(['\"])(.+?)\1\](?:\[(['\"])(.+?)\3\])?"
            r"(?:\s*=\s*\[?\]?\s*$|\.append\((.*))",
            line,
        )
        if not append_match or append_match.group(5) is None:
            # Handle `graph['x'] = []` reset lines — skip them
            i += 1
            continue

        top_key = append_match.group(2)
        sub_key = append_match.group(4)
        path = [top_key] if sub_key is None else [top_key, sub_key]
        value_str = append_match.group(5)

        # Check if the value is complete on this line (balanced parens/brackets)
        # or spans multiple lines.  We need to collect until the .append( call
        # is closed: the value_str ends with ')' and brackets are balanced.
        collected = value_str
        if not _is_balanced(collected):
            # Consume subsequent lines until balanced
            j = i + 1
            while j < len(lines) and not _is_balanced(collected):
                collected += "\n" + lines[j]
                j += 1
            i = j
        else:
            i += 1

        # Strip the trailing ')' that closes .append(...)
        if collected.endswith(")"):
            collected = collected[:-1]

        try:
            value = ast.literal_eval(collected)
        except (ValueError, SyntaxError):
            value = collected.strip().strip("'\"")

        updates.append({
            "key_phrase": current_key_phrase,
            "path": path,
            "value": value,
        })
        current_key_phrase = ""

    return updates


def _is_balanced(s: str) -> bool:
    """Check whether parentheses, brackets, and braces are balanced in s,
    and that there is at least one closing ')' to terminate the .append() call."""
    depth = {"(": 0, "[": 0, "{": 0}
    openers = {"(": "(", "[": "[", "{": "{"}
    closers = {")": "(", "]": "[", "}": "{"}
    in_string = None
    escape = False
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch in ("'", '"'):
            if in_string is None:
                in_string = ch
            elif ch == in_string:
                in_string = None
            continue
        if in_string:
            continue
        if ch in openers:
            depth[ch] += 1
        elif ch in closers:
            depth[closers[ch]] -= 1
    # Balanced when all depths are 0.  The trailing ')' from .append(value)
    # will push '(' depth to -1 once we've consumed enough lines, but we
    # stripped that context: we're checking the *value* portion only, which
    # should simply be balanced on its own AND end with ')' for the outer call.
    all_zero = all(v == 0 for v in depth.values())
    # The collected string includes the trailing ')' from .append(value),
    # so '(' depth will be -1 when complete.
    return all_zero or depth["("] == -1


def build_graph_state(updates: list[dict], up_to: int) -> dict:
    """Apply updates[0:up_to] to a scaffold containing all canonical parents."""
    # Start with the full scaffold so every parent is always present
    graph = _empty_graph_scaffold()
    for u in updates[:up_to]:
        path = u["path"]
        if len(path) == 1:
            graph.setdefault(path[0], []).append(u["value"])
        elif len(path) == 2:
            graph.setdefault(path[0], {}).setdefault(path[1], []).append(u["value"])
    return graph


def _empty_graph_scaffold() -> dict:
    """Return a graph dict with all canonical parent keys pre-populated (empty)."""
    return {
        "definition": [],
        "indicators": {k: [] for k in INDICATOR_SUBKEYS},
        "indicator_sequences": [],
        "answer_guesses": [],
        "operations": [],
    }


def compute_metrics_for_text(response_text: str) -> dict:
    """Compute breadth metrics from a single analysis response text."""
    updates = parse_graph_updates(response_text)
    if not updates:
        return {"total_ideas": 0, "by_category": {}, "update_count": 0}

    graph = build_graph_state(updates, len(updates))

    by_category = {}
    total = 0
    for key in ALL_PARENTS:
        val = graph.get(key)
        if isinstance(val, dict):
            cat_total = 0
            for sub_key, items in val.items():
                count = len(items)
                by_category[f"{key}.{sub_key}"] = count
                cat_total += count
            by_category[key] = cat_total
            total += cat_total
        elif isinstance(val, list):
            count = len(val)
            by_category[key] = count
            total += count

    return {
        "total_ideas": total,
        "by_category": by_category,
        "update_count": len(updates),
    }


def compute_and_save_metrics(output_dir: str):
    """Compute metrics from all *_cleaned.txt files and save metrics.json."""
    cleaned_files = sorted(glob.glob(os.path.join(output_dir, "*_cleaned.txt")))

    # Try to load existing index for token counts
    index_path = os.path.join(output_dir, "index.json")
    index = {}
    if os.path.isfile(index_path):
        index = json.loads(Path(index_path).read_text())

    per_problem = {}
    for clean_path in cleaned_files:
        stem = Path(clean_path).stem.replace("_cleaned", "")
        response_text = Path(clean_path).read_text()
        metrics = compute_metrics_for_text(response_text)

        # Attach token counts from index if available
        if stem in index and "tokens" in index[stem]:
            metrics["tokens"] = index[stem]["tokens"]
        else:
            metrics["tokens"] = {}

        per_problem[stem] = metrics

    # Aggregate
    all_totals = [m["total_ideas"] for m in per_problem.values()]
    all_tokens = [m["tokens"].get("total_tokens", 0) for m in per_problem.values() if m["tokens"]]

    # Collect all category keys
    all_cats = set()
    for m in per_problem.values():
        all_cats.update(m["by_category"].keys())

    cat_means = {}
    for cat in sorted(all_cats):
        vals = [m["by_category"].get(cat, 0) for m in per_problem.values()]
        cat_means[cat] = sum(vals) / len(vals) if vals else 0

    aggregate = {
        "num_problems": len(per_problem),
        "mean_total_ideas": sum(all_totals) / len(all_totals) if all_totals else 0,
        "mean_total_tokens": sum(all_tokens) / len(all_tokens) if all_tokens else 0,
        "mean_by_category": cat_means,
    }

    metrics_data = {
        "per_problem": per_problem,
        "aggregate": aggregate,
    }

    metrics_path = os.path.join(output_dir, "metrics.json")
    Path(metrics_path).write_text(json.dumps(metrics_data, indent=2))
    print(f"  Saved: {metrics_path} ({len(per_problem)} problems)")
    return metrics_data


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 5: Visualization
# ═══════════════════════════════════════════════════════════════════════════════

def render_graph(graph: dict, title: str, step: int, out_path: str):
    """Render the graph as a tree diagram. All canonical parents are always shown."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(16, 10))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_title(f"{title} — Step {step}", fontsize=13, fontweight="bold", pad=15)

    colors = {
        "definition": "#4C72B0",
        "indicators": "#DD8452",
        "indicator_sequences": "#55A868",
        "answer_guesses": "#8172B3",
        "operations": "#C44E52",
    }
    fallback_colors = ["#8172B3", "#937860", "#DA8BC3", "#8C8C8C"]

    # Always use the canonical parent ordering
    top_keys = ALL_PARENTS[:]
    # Add any extra keys that aren't canonical
    for k in graph:
        if k not in top_keys:
            top_keys.append(k)

    n_top = len(top_keys)
    x_positions = [(i + 0.5) / max(n_top, 1) for i in range(n_top)]
    root_y = 0.92

    for col_idx, (top_key, x_center) in enumerate(zip(top_keys, x_positions)):
        color = colors.get(top_key, fallback_colors[col_idx % len(fallback_colors)])
        children = graph.get(top_key)

        # Determine if this parent has any content
        has_content = False
        if isinstance(children, dict):
            has_content = any(len(v) > 0 for v in children.values())
        elif isinstance(children, list):
            has_content = len(children) > 0

        # Draw top-level node (dimmed if empty)
        alpha = 0.9 if has_content else 0.3
        ax.text(x_center, root_y, top_key, ha="center", va="center",
                fontsize=11, fontweight="bold", color="white",
                bbox=dict(boxstyle="round,pad=0.4", facecolor=color,
                          edgecolor="none", alpha=alpha))

        if children is None:
            continue

        if isinstance(children, dict):
            sub_keys = list(children.keys())
            n_sub = len(sub_keys)
            if n_sub == 0:
                continue
            col_width = 0.85 / max(n_top, 1)
            sub_x_positions = [
                x_center + (j - (n_sub - 1) / 2) * (col_width / max(n_sub, 1))
                for j in range(n_sub)
            ]
            sub_y = root_y - 0.15

            for sub_idx, (sub_key, sx) in enumerate(zip(sub_keys, sub_x_positions)):
                leaves = children[sub_key]
                sub_alpha = 0.15 if leaves else 0.06
                text_alpha = 1.0 if leaves else 0.35

                ax.plot([x_center, sx], [root_y - 0.04, sub_y + 0.03],
                        color=color, alpha=0.4 if leaves else 0.15, linewidth=1.5)
                ax.text(sx, sub_y, sub_key, ha="center", va="center",
                        fontsize=9, fontweight="bold", color=color, alpha=text_alpha,
                        bbox=dict(boxstyle="round,pad=0.3", facecolor=color,
                                  edgecolor="none", alpha=sub_alpha))

                for leaf_idx, leaf in enumerate(leaves):
                    leaf_y = sub_y - 0.08 - leaf_idx * 0.06
                    if leaf_y < 0.02:
                        ax.text(sx, leaf_y + 0.06, "...", ha="center", fontsize=8, color="gray")
                        break
                    ax.plot([sx, sx], [sub_y - 0.03, leaf_y + 0.01],
                            color=color, alpha=0.25, linewidth=1)
                    label = str(leaf) if len(str(leaf)) < 40 else str(leaf)[:37] + "..."
                    ax.text(sx, leaf_y, label, ha="center", va="center",
                            fontsize=7.5, color="#333",
                            bbox=dict(boxstyle="round,pad=0.2", facecolor="#f0f0f0",
                                      edgecolor=color, alpha=0.6, linewidth=0.5))

        elif isinstance(children, list):
            for leaf_idx, leaf in enumerate(children):
                leaf_y = root_y - 0.12 - leaf_idx * 0.06
                if leaf_y < 0.02:
                    ax.text(x_center, leaf_y + 0.06, "...", ha="center", fontsize=8, color="gray")
                    break
                ax.plot([x_center, x_center], [root_y - 0.04, leaf_y + 0.01],
                        color=color, alpha=0.3, linewidth=1)
                label = str(leaf) if len(str(leaf)) < 40 else str(leaf)[:37] + "..."
                ax.text(x_center, leaf_y, label, ha="center", va="center",
                        fontsize=8, color="#333",
                        bbox=dict(boxstyle="round,pad=0.2", facecolor="#f0f0f0",
                                  edgecolor=color, alpha=0.6, linewidth=0.5))

    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def visualize_idea_graph(response_text: str, stem: str, output_dir: str):
    """Parse graph updates from a response and generate step-by-step images + GIF."""
    from PIL import Image

    updates = parse_graph_updates(response_text)
    if not updates:
        print(f"  No graph updates found for {stem}")
        return

    vis_dir = os.path.join(output_dir, f"{stem}_vis")
    os.makedirs(vis_dir, exist_ok=True)

    image_paths = []
    for step in range(1, len(updates) + 1):
        graph = build_graph_state(updates, step)
        img_path = os.path.join(vis_dir, f"step_{step:03d}.png")
        render_graph(graph, stem, step, img_path)
        image_paths.append(img_path)

    if image_paths:
        frames = [Image.open(p) for p in image_paths]
        gif_path = os.path.join(vis_dir, f"{stem}_evolution.gif")
        durations = [1500] * len(frames)
        durations[-1] = 4000
        frames[0].save(
            gif_path, save_all=True, append_images=frames[1:],
            duration=durations, loop=0,
        )
        print(f"  Visualization: {len(image_paths)} steps → {gif_path}")


def visualize_all_in_dir(output_dir: str, file: str | None = None):
    """Visualize both raw and cleaned versions for each problem.

    Generates <stem>_vis/ (cleaned) and <stem>_vis_raw/ (pre-cleaned)
    so the two GIFs can be compared side-by-side.
    """
    if file:
        txt_path = os.path.join(output_dir, file) if not os.path.isabs(file) else file
        if not os.path.isfile(txt_path):
            print(f"File not found: {txt_path}")
            return
        response = Path(txt_path).read_text()
        stem = Path(txt_path).stem.replace("_cleaned", "").replace("_analysis", "")
        visualize_idea_graph(response, stem, output_dir)
    else:
        # Visualize cleaned versions
        cleaned_files = sorted(glob.glob(os.path.join(output_dir, "*_cleaned.txt")))
        raw_files = sorted(glob.glob(os.path.join(output_dir, "*_analysis.txt")))

        if not cleaned_files and not raw_files:
            print(f"No analysis files found in {output_dir}")
            return

        # Always visualize raw versions (into <stem>_vis_raw/)
        for txt_path in raw_files:
            response = Path(txt_path).read_text()
            stem = Path(txt_path).stem.replace("_analysis", "")
            visualize_idea_graph(response, f"{stem}_raw", output_dir)

        # Visualize cleaned versions (into <stem>_vis/)
        if cleaned_files:
            for txt_path in cleaned_files:
                response = Path(txt_path).read_text()
                stem = Path(txt_path).stem.replace("_cleaned", "")
                visualize_idea_graph(response, stem, output_dir)

        n_raw = len(raw_files)
        n_cleaned = len(cleaned_files)
        print(f"  Generated {n_raw} raw + {n_cleaned} cleaned visualizations")


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _save_diff(raw_text: str, cleaned_text: str, diff_path: str):
    """Compare the final graph states of raw vs cleaned and report per-category diffs.

    For each category, reports which items were removed and which (if any) were added.
    Uses multiset (Counter) comparison so duplicate values are handled correctly.
    """
    from collections import Counter

    raw_graph = build_graph_state(
        parse_graph_updates(raw_text),
        len(parse_graph_updates(raw_text)),
    )
    cleaned_graph = build_graph_state(
        parse_graph_updates(cleaned_text),
        len(parse_graph_updates(cleaned_text)),
    )

    lines = []

    # Collect all leaf paths: (top_key,) or (top_key, sub_key)
    all_paths = set()
    for g in (raw_graph, cleaned_graph):
        for top_key, val in g.items():
            if isinstance(val, dict):
                for sub_key in val:
                    all_paths.add((top_key, sub_key))
            elif isinstance(val, list):
                all_paths.add((top_key,))

    for path in sorted(all_paths):
        label = ".".join(path)

        if len(path) == 1:
            raw_items = raw_graph.get(path[0], [])
            clean_items = cleaned_graph.get(path[0], [])
        else:
            raw_items = raw_graph.get(path[0], {}).get(path[1], [])
            clean_items = cleaned_graph.get(path[0], {}).get(path[1], [])

        # Stringify for comparison (values can be dicts, tuples, etc.)
        raw_counts = Counter(repr(v) for v in raw_items)
        clean_counts = Counter(repr(v) for v in clean_items)

        removed_counts = raw_counts - clean_counts
        added_counts = clean_counts - raw_counts

        if not removed_counts and not added_counts:
            continue

        lines.append(f"[{label}]  ({len(raw_items)} → {len(clean_items)})")
        for item, count in sorted(removed_counts.items()):
            suffix = f" (x{count})" if count > 1 else ""
            lines.append(f"  - {item}{suffix}")
        for item, count in sorted(added_counts.items()):
            suffix = f" (x{count})" if count > 1 else ""
            lines.append(f"  + {item}{suffix}")
        lines.append("")

    if not lines:
        lines.append("(no differences — nothing was removed)")

    Path(diff_path).write_text("\n".join(lines))


def _find_md_files(experiment_dir: str) -> list[str]:
    md_files = sorted(glob.glob(os.path.join(experiment_dir, "**", "*.md"), recursive=True))
    md_files = [f for f in md_files if "idea_graph_analysis" not in f]
    if not md_files:
        print(f"No .md files found in {experiment_dir}")
    else:
        print(f"Found {len(md_files)} .md files in {experiment_dir}")
    return md_files


def _save_index(output_dir: str, results: dict):
    index_path = os.path.join(output_dir, "index.json")
    # Strip non-serializable keys but keep everything else
    index = {}
    for k, v in results.items():
        index[k] = {kk: vv for kk, vv in v.items() if kk != "response"}
    Path(index_path).write_text(json.dumps(index, indent=2))
    print(f"\nIndex saved: {index_path}")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Idea Graph Analysis Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("experiment_dir",
                        help="Path to experiment_logs subdirectory")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="Output directory (default: <experiment_dir>/idea_graph_analysis)")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--clean-only", action="store_true",
                      help="Re-clean existing raw analyses (skip extraction)")
    mode.add_argument("--metrics-only", action="store_true",
                      help="Recompute metrics from existing cleaned analyses")
    mode.add_argument("--visualize-only", nargs="?", const=True, default=False,
                      metavar="FILE",
                      help="Visualize existing cleaned analyses. "
                           "Optionally specify a single file.")

    args = parser.parse_args()

    output_dir = args.output_dir or os.path.join(
        os.path.abspath(args.experiment_dir), "idea_graph_analysis"
    )

    if args.clean_only:
        run_clean_only(output_dir)
        print("\n── Re-visualizing ──")
        visualize_all_in_dir(output_dir)
    elif args.metrics_only:
        compute_and_save_metrics(output_dir)
    elif args.visualize_only is not False:
        file = args.visualize_only if args.visualize_only is not True else None
        visualize_all_in_dir(output_dir, file=file)
    else:
        run_analysis(args.experiment_dir, output_dir)


if __name__ == "__main__":
    main()
