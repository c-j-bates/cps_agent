from __future__ import annotations

"""
Idea coding pipeline: extract solver outputs and code them into faceted traces.

Stages:
  1. Extract LLM outputs from experiment log .md files
  2. Run Prompt A (initial coding) → raw trace
  3. Run Prompt B (dedup & normalization) → cleaned trace
  4. Run span resolution (deterministic) → spans added
"""

import ast
import json
import os
import re
from pathlib import Path

from .prompts import PROMPT_A, PROMPT_B, PROMPT_C


# ═══════════════════════════════════════════════════════════════════════════════
# Extraction from experiment logs (reused from old pipeline)
# ═══════════════════════════════════════════════════════════════════════════════

def parse_calls(md_text: str) -> list[dict]:
    """Parse all LLM calls from an experiment log .md file."""
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
    for fence in ["````", "```"]:
        pattern = re.compile(
            rf"\*\*{label}\*\*:\s*\n\n{fence}\n(.*?){fence}",
            re.DOTALL,
        )
        m = pattern.search(section)
        if m:
            return m.group(1).rstrip("\n")
    return ""


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

    parts = []
    parts.append(calls[last_main_idx]["prompt"])
    parts.append(f"\n\n{calls[last_main_idx]['response']}")
    for c in calls[last_main_idx + 1:]:
        parts.append(f"\n\n{c['prompt']}")
        parts.append(f"\n\n{c['response']}")
    return "\n".join(parts)


def extract_clue_text(md_path: str) -> str:
    """Extract the raw clue string from the Problem Statement section."""
    md_text = Path(md_path).read_text()
    m = re.search(r"## Problem Statement\s*\n+```\n(.+?)\n```", md_text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def extract_token_counts(md_path: str) -> dict:
    """Extract token counts from the Token Summary table."""
    md_text = Path(md_path).read_text()
    counts = {}
    for key, pattern in [
        ("total_input_tokens", r"\|\s*Total input tokens\s*\|\s*([\d,]+)\s*\|"),
        ("total_output_tokens", r"\|\s*Total output tokens\s*\|\s*([\d,]+)\s*\|"),
        ("total_tokens", r"\|\s*Total tokens\s*\|\s*([\d,]+)\s*\|"),
    ]:
        m = re.search(pattern, md_text)
        counts[key] = int(m.group(1).replace(",", "")) if m else 0
    return counts


def extract_problem_id(md_path: str) -> int | None:
    """Extract problem ID from metadata."""
    md_text = Path(md_path).read_text(errors="replace")
    m = re.search(r"\*\*Problem ID\*\*:\s*(\d+)", md_text)
    return int(m.group(1)) if m else None


def extract_solver_answer(md_path: str) -> str | None:
    """Extract the solver's final answer from the companion .json file."""
    json_path = Path(md_path).with_suffix(".json")
    if json_path.is_file():
        try:
            data = json.loads(json_path.read_text())
            return data.get("final_answer")
        except (json.JSONDecodeError, KeyError):
            pass
    return None


# Cache for the dataset CSV (loaded once)
_solution_cache: dict[str, str] = {}


def _load_solution_cache(dataset_dir: str = "datasets"):
    """Load solution lookup from all CSV files in the datasets directory."""
    if _solution_cache:
        return
    dataset_path = Path(dataset_dir)
    if not dataset_path.is_dir():
        # Try relative to the package
        dataset_path = Path(__file__).parent.parent / dataset_dir
    for csv_file in dataset_path.glob("*.csv"):
        try:
            import csv
            with open(csv_file, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    problem = row.get("problem", "").strip().strip('"')
                    solution = row.get("solution", "").strip()
                    if problem and solution:
                        _solution_cache[problem] = solution
        except Exception:
            continue


def lookup_correct_solution(raw_clue: str) -> str | None:
    """Look up the correct solution for a clue from the dataset CSV."""
    _load_solution_cache()
    # Try exact match first
    clue = raw_clue.strip().strip('"')
    if clue in _solution_cache:
        return _solution_cache[clue]
    # Try without quotes
    for key, val in _solution_cache.items():
        if key.strip('"') == clue or clue in key or key in clue:
            return val
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Clue text parsing
# ═══════════════════════════════════════════════════════════════════════════════

def strip_enumeration(clue: str) -> tuple[str, str]:
    """Strip the enumeration (e.g. '(5)' or '(3, 2)') from a clue.

    Returns (clue_text, enumeration) where clue_text has the enum and
    surrounding whitespace/quotes removed.
    """
    # Strip surrounding quotes if present
    clue = clue.strip().strip('"').strip("'")
    m = re.search(r'\s*\([\d,\s]+\)\s*$', clue)
    if m:
        enum = clue[m.start():].strip()
        text = clue[:m.start()].strip()
        # Extract just the numbers
        enum_inner = re.search(r'\(([\d,\s]+)\)', enum)
        return text, enum_inner.group(1).strip() if enum_inner else ""
    return clue.strip(), ""


# ═══════════════════════════════════════════════════════════════════════════════
# Trace parsing
# ═══════════════════════════════════════════════════════════════════════════════

def parse_trace_from_response(response_text: str) -> list[tuple]:
    """Parse trace.append(...) calls from an LLM response.

    Returns list of (tree_id, parent_path, facets_dict) tuples.

    When the LLM "redoes" its output (multiple code blocks each starting
    with ``trace = []``), only the *last* block that reinitializes trace
    is used, so earlier drafts don't leak through.
    """
    code_blocks = re.findall(r"```python\n(.*?)```", response_text, re.DOTALL)
    if not code_blocks:
        code_blocks = [response_text]

    # Use only the last code block that contains a trace initialization,
    # plus any subsequent blocks (which may continue appending).
    last_init = 0
    for i, block in enumerate(code_blocks):
        if re.search(r'trace\s*=\s*\[', block):
            last_init = i
    code_blocks = code_blocks[last_init:]

    full_code = "\n".join(code_blocks)

    # Execute in a sandboxed namespace. The code block typically contains
    # `trace = []` followed by `trace.append(...)` calls, so we let exec
    # create the variable rather than pre-assigning it.
    namespace = {}
    try:
        exec(full_code, namespace)
        trace = namespace.get("trace", [])
    except Exception:
        # Fallback: try to parse individual append calls
        trace = _parse_appends_fallback(full_code)

    return trace


def _parse_appends_fallback(code: str) -> list[tuple]:
    """Regex-based fallback for parsing trace.append() calls."""
    trace = []
    # Match trace.append(...) spanning multiple lines
    pattern = re.compile(r'trace\.append\((.*?)\)\s*$', re.DOTALL | re.MULTILINE)

    lines = code.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        if 'trace.append(' in line:
            # Collect lines until balanced
            collected = line
            depth = collected.count('(') - collected.count(')')
            j = i + 1
            while depth > 0 and j < len(lines):
                collected += '\n' + lines[j]
                depth = collected.count('(') - collected.count(')')
                j += 1
            i = j

            # Extract the argument
            m = re.search(r'trace\.append\((.+)\)\s*(?:#.*)?$', collected, re.DOTALL)
            if m:
                try:
                    val = ast.literal_eval(m.group(1).strip())
                    trace.append(val)
                except (ValueError, SyntaxError):
                    pass
        else:
            i += 1

    return trace


# ═══════════════════════════════════════════════════════════════════════════════
# Generate tree de novo, ignoring how LLM coder decided to do it
# ═══════════════════════════════════════════════════════════════════════════════


_FACET_KEYS = ("parse", "mechanism", "execution", "output")


def _count_non_null(facets: dict) -> int:
    return sum(facets.get(k) is not None for k in _FACET_KEYS)


def _facet_key(facets: dict, name: str):
    """Canonical comparable form of a single facet value."""
    v = facets.get(name)
    if v is None:
        return None
    if isinstance(v, list):
        return tuple(str(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((str(k2), str(v2)) for k2, v2 in v.items()))
    return str(v)


def _is_legal_parent(child: dict, parent: dict) -> bool:
    """True if parent's non-null facets are a strict subset of child's,
    with matching values on shared facets."""
    parent_nn = {k for k in _FACET_KEYS if parent.get(k) is not None}
    child_nn = {k for k in _FACET_KEYS if child.get(k) is not None}

    if not parent_nn:
        return False  # all-null nodes are filtered before tree-building

    # Parent's non-null keys must be a strict subset of child's
    if not parent_nn < child_nn:
        return False

    # Shared facets must match
    return all(_facet_key(parent, k) == _facet_key(child, k) for k in parent_nn)


class _TreeNode:
    """Internal node for tree reconstruction."""
    __slots__ = ("facets", "children", "non_null_count", "orig_idx")

    def __init__(self, facets: dict, orig_idx: int = 0):
        self.facets = facets
        self.children: list[_TreeNode] = []
        self.non_null_count = _count_non_null(facets)
        self.orig_idx = orig_idx  # position in the original trace


def _find_best_parent(child_facets: dict, roots: list[_TreeNode]) -> _TreeNode | None:
    """Walk the forest to find the most specific legal parent for child_facets.

    'Most specific' = most non-null facets (deepest valid ancestor).
    """
    best = None
    best_count = -1

    def _search(node: _TreeNode):
        nonlocal best, best_count
        if _is_legal_parent(child_facets, node.facets):
            if node.non_null_count > best_count:
                best = node
                best_count = node.non_null_count
            # Search children for something even more specific
            for c in node.children:
                _search(c)

    for root in roots:
        _search(root)
    return best


def _serialize_forest(roots: list[_TreeNode]) -> list:
    """Walk the forest and emit [tree_id, path, facets] entries.

    Entries are sorted by their original trace index so the animation
    replays the solver's ideation order.
    """
    entries = []  # (orig_idx, tree_id, path, facets)

    def _walk(node: _TreeNode, tree_id: str, path: list):
        entries.append((node.orig_idx, tree_id, list(path), node.facets))
        # Sort children by orig_idx so child indices are stable
        for i, child in enumerate(sorted(node.children, key=lambda c: c.orig_idx)):
            _walk(child, tree_id, path + [i])

    for ti, root in enumerate(sorted(roots, key=lambda r: r.orig_idx)):
        _walk(root, f"tree{ti + 1}", [])

    # Sort output by original trace position for animation ordering
    entries.sort(key=lambda e: e[0])
    return [[tid, path, facets] for _, tid, path, facets in entries]


def _shared_facets(a: dict, b: dict) -> dict:
    """Return dict of facets where both a and b are non-null with matching values."""
    shared = {}
    for k in _FACET_KEYS:
        if a.get(k) is not None and b.get(k) is not None:
            if _facet_key(a, k) == _facet_key(b, k):
                shared[k] = a[k]
    return shared


def _spawn_synthetic_parents(roots: list[_TreeNode]) -> list[_TreeNode]:
    """Group roots sharing facet values under synthetic parent nodes.

    Greedy algorithm:
      1. Find the pair of roots with the most shared non-null, matching facets.
      2. Create a synthetic parent (or reuse an existing root) with those facets.
      3. Re-parent ALL roots that the synthetic can legally parent.
      4. Repeat until no pair of roots shares any facets.
    """
    MAX_ITER = 50  # safety bound

    for _ in range(MAX_ITER):
        # Find pair of roots with most shared facets
        best_shared: dict = {}
        best_count = 0
        for i in range(len(roots)):
            for j in range(i + 1, len(roots)):
                shared = _shared_facets(roots[i].facets, roots[j].facets)
                if len(shared) > best_count:
                    best_count = len(shared)
                    best_shared = shared

        if best_count == 0:
            break

        synth_facets = {k: best_shared.get(k) for k in _FACET_KEYS}
        synth_nn = {k for k in _FACET_KEYS if synth_facets.get(k) is not None}

        # Check if an existing root already has exactly these facets
        parent_node: _TreeNode | None = None
        for root in roots:
            root_nn = {k for k in _FACET_KEYS if root.facets.get(k) is not None}
            if root_nn == synth_nn:
                if all(_facet_key(root.facets, k) == _facet_key(synth_facets, k)
                       for k in root_nn):
                    parent_node = root
                    break

        # Partition roots into children vs remaining
        children: list[_TreeNode] = []
        remaining: list[_TreeNode] = []
        for root in roots:
            if root is parent_node:
                continue  # don't parent yourself
            if _is_legal_parent(root.facets, synth_facets):
                children.append(root)
            else:
                remaining.append(root)

        # Need ≥2 children to justify a new synthetic, or ≥1 new child
        # if reusing an existing root
        if parent_node is None and len(children) < 2:
            break
        if parent_node is not None and len(children) < 1:
            break

        if parent_node is None:
            # Temporary orig_idx; will be re-indexed below
            min_orig = min(c.orig_idx for c in children)
            parent_node = _TreeNode(synth_facets, orig_idx=min_orig)
            parent_node.children = children
        else:
            parent_node.children.extend(children)

        remaining.append(parent_node)
        roots = remaining

    # Re-index so synthetic parents get a proper integer orig_idx
    # just before their first child, preserving overall order.
    _reindex_forest(roots)

    return roots


def _reindex_forest(roots: list[_TreeNode]) -> None:
    """Assign fresh integer orig_idx values to every node.

    Walks the forest in (orig_idx) order, assigning 0, 1, 2, ...
    Parents are visited before children so synthetic parents
    naturally precede their subtrees.
    """
    counter = 0

    def _walk(node: _TreeNode):
        nonlocal counter
        node.orig_idx = counter
        counter += 1
        for child in sorted(node.children, key=lambda c: c.orig_idx):
            _walk(child)

    for root in sorted(roots, key=lambda r: r.orig_idx):
        _walk(root)


def clean_trace(trace: list) -> list:
    """Rebuild the tree structure from facet dicts alone.

    Input items look like:
        [tree_id, coord, facets]

    We ignore the original tree_id/coord and only use the facet dicts.

    Two-phase tree construction:
      Phase 1 — Direct parenting: A node P can parent C iff P's non-null
        facets are a strict subset of C's with matching values. Among valid
        parents, choose the most specific (most non-null facets).
      Phase 2 — Synthetic parents: roots that share facet values but can't
        directly parent each other are grouped under a new synthetic node
        holding their common facets.

    Output has the same shape as the input:
        [new_tree_id, new_coord, facets]
    """
    if not trace:
        return trace

    # Extract (original_index, facets), drop all-null entries,
    # sort by non-null count (roots first)
    indexed_facets = []
    for i, entry in enumerate(trace):
        if isinstance(entry, (list, tuple)) and len(entry) >= 3:
            f = entry[2] if isinstance(entry[2], dict) else {}
            if _count_non_null(f) == 0:
                continue  # skip ideas with no facets at all
            indexed_facets.append((i, f))

    indexed_facets.sort(key=lambda x: _count_non_null(x[1]))

    # Phase 1: direct parenting
    roots: list[_TreeNode] = []

    for orig_idx, facets in indexed_facets:
        parent = _find_best_parent(facets, roots)
        node = _TreeNode(facets, orig_idx=orig_idx)
        if parent is not None:
            parent.children.append(node)
        else:
            roots.append(node)

    # Phase 2: spawn synthetic parents for orphaned roots that share facets
    roots = _spawn_synthetic_parents(roots)

    return _serialize_forest(roots)


# ═══════════════════════════════════════════════════════════════════════════════
# Span resolution (Section 10.4 of spec)
# ═══════════════════════════════════════════════════════════════════════════════

def resolve_spans(trace: list, clue_text: str) -> list:
    """Add 'span': [start, end] to every {'text': ...} dict in the trace.

    Operates in-place and returns the trace.
    """
    for entry in trace:
        if len(entry) < 3:
            continue
        facets = entry[2] if isinstance(entry[2], dict) else {}

        # Parse spans
        p = facets.get("parse")
        if isinstance(p, dict) and p.get("text") is not None:
            text = p["text"]
            if p.get("position") == "start":
                i = clue_text.find(text)
            elif p.get("position") == "end":
                i = clue_text.rfind(text)
            else:
                i = clue_text.find(text)
            p["span"] = [i, i + len(text)] if i >= 0 else None

        # Execution spans
        ex = facets.get("execution")
        if isinstance(ex, list):
            for step in ex:
                if not isinstance(step, dict):
                    continue
                for key in ["indicator", "fodder", "outer", "inner"]:
                    ref = step.get(key)
                    if isinstance(ref, dict) and ref.get("text") is not None:
                        t = ref["text"]
                        i = clue_text.find(t)
                        ref["span"] = [i, i + len(t)] if i >= 0 else None

    return trace


# ═══════════════════════════════════════════════════════════════════════════════
# Single-problem coding pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def code_one(md_path: str, cache_dir: str, client=None,
             coder_model: str = "claude-opus-4-6",
             coder_provider: str = "claude",
             coder_thinking: bool | str = False) -> dict | None:
    """Code a single experiment log .md file into a faceted trace.

    Cache hierarchy (avoids redundant LLM calls):
      1. {stem}_cleaned.json with non-empty trace → return immediately.
      2. {stem}_cleaned_trace.txt exists → re-parse and rebuild .json
         (no LLM calls needed).
      3. Otherwise → run Prompt A and Prompt B, then parse.

    Returns dict with keys: trace, clue_text, clue_enum, tokens, problem_id.
    """
    stem = Path(md_path).stem
    os.makedirs(cache_dir, exist_ok=True)

    clean_json_path = os.path.join(cache_dir, f"{stem}_cleaned.json")
    cleaned_txt_path = os.path.join(cache_dir, f"{stem}_cleaned_trace.txt")
    raw_txt_path = os.path.join(cache_dir, f"{stem}_raw_trace.txt")

    # ── Extract metadata (always needed) ────────────────────────────────
    raw_clue = extract_clue_text(md_path)
    if not raw_clue:
        print(f"  [{stem}] skipping (no clue text found)")
        return None
    clue_text, clue_enum = strip_enumeration(raw_clue)
    tokens = extract_token_counts(md_path)
    problem_id = extract_problem_id(md_path)
    solver_answer = extract_solver_answer(md_path)
    correct_solution = lookup_correct_solution(raw_clue)

    # ── Cache level 1: full JSON with non-empty trace ───────────────────
    if os.path.isfile(clean_json_path):
        data = json.loads(Path(clean_json_path).read_text())
        if data.get("trace"):
            # Backfill solution fields if missing from older cached files
            changed = False
            if "solver_answer" not in data and solver_answer:
                data["solver_answer"] = solver_answer
                changed = True
            if "correct_solution" not in data and correct_solution:
                data["correct_solution"] = correct_solution
                changed = True
            if changed:
                Path(clean_json_path).write_text(json.dumps(data, indent=2))
            print(f"  [{stem}] cached ({len(data['trace'])} entries)")
            return data

    # ── Cache level 2: LLM text outputs exist, just re-parse ───────────
    if os.path.isfile(cleaned_txt_path):
        print(f"  [{stem}] re-parsing from cached LLM output")
        cleaned_response = Path(cleaned_txt_path).read_text()

    # ── No cache: run LLM calls ─────────────────────────────────────────
    else:
        llm_outputs = extract_llm_outputs(md_path)
        if not llm_outputs:
            print(f"  [{stem}] skipping (no main-channel calls)")
            return None

        if client is None:
            from llm_clients import create_client
            client = create_client(coder_provider, model=coder_model,
                                   thinking=coder_thinking, temperature=0.3)

        # Stage 1: Prompt A - initial coding
        prompt_a = PROMPT_A.format(
            clue_text=clue_text,
            clue_enum=clue_enum,
            llm_outputs=llm_outputs,
        )
        print(f"  [{stem}] coding (Prompt A)...")
        raw_response = str(client(prompt_a))
        Path(raw_txt_path).write_text(raw_response)

        # Stage 2: Prompt B - dedup & normalization
        prompt_b = PROMPT_B.format(
            clue_text=clue_text,
            raw_trace=raw_response,
        )
        print(f"  [{stem}] cleaning (Prompt B)...")
        cleaned_response = str(client(prompt_b))
        Path(cleaned_txt_path).write_text(cleaned_response)

    # ── Common post-processing: parse → rebuild trees → resolve spans ──
    trace = parse_trace_from_response(cleaned_response)
    trace = clean_trace(trace)
    trace = resolve_spans(trace, clue_text)

    result = {
        "trace": _serialize_trace(trace),
        "clue_text": clue_text,
        "clue_enum": clue_enum,
        "raw_clue": raw_clue,
        "tokens": tokens,
        "problem_id": problem_id,
        "md_path": md_path,
        "solver_answer": solver_answer,
        "correct_solution": correct_solution,
    }

    Path(clean_json_path).write_text(json.dumps(result, indent=2))
    print(f"  [{stem}] built JSON ({len(trace)} entries)")
    return result


def _serialize_trace(trace: list) -> list:
    """Convert trace tuples to JSON-serializable lists."""
    serialized = []
    for entry in trace:
        if isinstance(entry, tuple):
            entry = list(entry)
        serialized.append(entry)
    return serialized


# ═══════════════════════════════════════════════════════════════════════════════
# Batch coding
# ═══════════════════════════════════════════════════════════════════════════════

def find_md_files(experiment_log_dir: str) -> list[str]:
    """Find all .md experiment log files in a directory (recursively)."""
    exp_dir = Path(experiment_log_dir)
    md_files = []
    for subdir in sorted(exp_dir.iterdir()):
        if not subdir.is_dir():
            continue
        md_file = subdir / f"{subdir.name}.md"
        if md_file.is_file():
            md_files.append(str(md_file))
    return md_files


def code_experiment(experiment_log_dir: str, output_base_dir: str,
                    client=None, max_problems: int | None = None,
                    coder_model: str = "claude-opus-4-6",
                    coder_provider: str = "claude",
                    coder_thinking: bool | str = False) -> list[dict]:
    """Code all problems in an experiment log directory.

    Caches per-problem results in output_base_dir/<experiment_name>/coding/.
    Returns list of coded results.

    Args:
        max_problems: If set, only process the first N .md files.
        coder_model: LLM model name for coding calls.
        coder_provider: LLM provider for coding calls.
    """
    experiment_log_dir = os.path.abspath(experiment_log_dir)
    experiment_name = Path(experiment_log_dir).name

    cache_dir = os.path.join(output_base_dir, experiment_name, "coding")
    os.makedirs(cache_dir, exist_ok=True)

    md_files = find_md_files(experiment_log_dir)
    if max_problems:
        md_files = md_files[:max_problems]
    if not md_files:
        print(f"  No .md files found in {experiment_log_dir}")
        return []

    results = []
    for md_path in md_files:
        print(f"\n  Processing: {os.path.basename(md_path)}")
        result = code_one(md_path, cache_dir, client=client,
                          coder_model=coder_model,
                          coder_provider=coder_provider,
                          coder_thinking=coder_thinking)
        if result is not None:
            results.append(result)

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Novel type review (Prompt C - batch)
# ═══════════════════════════════════════════════════════════════════════════════

def collect_novel_types(all_results: list[dict]) -> list[dict]:
    """Collect all novel_ mechanism types across all coded results.

    Returns list of {type, contexts} dicts.
    """
    novel_map = {}  # type_name -> list of context strings
    for result in all_results:
        trace = result.get("trace", [])
        clue = result.get("clue_text", "")
        for entry in trace:
            facets = entry[2] if len(entry) >= 3 and isinstance(entry[2], dict) else {}
            mechanism = facets.get("mechanism")
            if mechanism is None:
                continue
            mechs = mechanism if isinstance(mechanism, list) else [mechanism]
            for m in mechs:
                if isinstance(m, str) and m.startswith("novel_"):
                    if m not in novel_map:
                        novel_map[m] = []
                    novel_map[m].append(clue)

    return [{"type": t, "contexts": cs} for t, cs in novel_map.items()]


def review_novel_types(novel_types: list[dict], client=None) -> list[dict]:
    """Run Prompt C to review novel mechanism types.

    Returns list of decision dicts.
    """
    if not novel_types:
        return []

    if client is None:
        from llm_clients import AnthropicClient
        client = AnthropicClient(model="claude-sonnet-4-20250514", temperature=0.3)

    types_text = "\n".join(
        f"- {t['type']}: used in {len(t['contexts'])} clues. "
        f"Example: \"{t['contexts'][0]}\""
        for t in novel_types
    )

    prompt = PROMPT_C.format(novel_types=types_text)
    print("  Reviewing novel types (Prompt C)...")
    response = str(client(prompt))

    # Parse JSON from response
    try:
        m = re.search(r'\[.*\]', response, re.DOTALL)
        if m:
            return json.loads(m.group())
    except json.JSONDecodeError:
        pass

    return []


def apply_novel_type_decisions(all_results: list[dict],
                               decisions: list[dict]) -> list[dict]:
    """Apply Prompt C decisions to all traces.

    Modifies traces in-place and returns them.
    """
    remap = {}
    decompose = {}
    for d in decisions:
        action = d.get("action")
        if action == "remap":
            remap[d["from"]] = d["to"]
        elif action == "decompose":
            decompose[d["from"]] = d["to"]
        elif action == "merge":
            canonical = d["canonical"]
            for t in d.get("types", []):
                remap[t] = canonical

    for result in all_results:
        trace = result.get("trace", [])
        for entry in trace:
            facets = entry[2] if len(entry) >= 3 and isinstance(entry[2], dict) else {}
            mechanism = facets.get("mechanism")
            if mechanism is None:
                continue
            mechs = mechanism if isinstance(mechanism, list) else [mechanism]
            new_mechs = []
            for m in mechs:
                if m in remap:
                    new_mechs.append(remap[m])
                elif m in decompose:
                    new_mechs.extend(decompose[m])
                else:
                    new_mechs.append(m)
            facets["mechanism"] = new_mechs

    return all_results
