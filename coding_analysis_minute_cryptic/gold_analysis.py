"""
Gold idea analysis: compare strategy ideation against gold-standard solutions.

Stage 1: Extract candidate gold ideas from successful solves.
Stage 1b: Code gold ideas from opus experiment logs (authoritative solver).
Stage 2: After manual curation, compare each strategy's ideas against the gold
         and report per-facet coverage (which gold facets were/weren't matched).
"""

from __future__ import annotations

import ast
import glob
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from .coding import _FACET_KEYS, _facet_key, strip_enumeration
from .prompts import PROMPT_GOLD


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 1: Extract candidate gold ideas from successful solves
# ═══════════════════════════════════════════════════════════════════════════════

def extract_gold_candidates(
    strategy_by_problem: dict[str, dict[int, dict]],
) -> dict[int, list[dict]]:
    """Extract candidate gold ideas from successful solves.

    For each problem, finds the most complete idea (most non-null facets,
    with output matching the correct solution) from each successful strategy.

    Returns: {problem_id: [candidate_dicts]}
    where each candidate_dict has:
        strategy, solver_answer, correct_solution, facets, raw_clue
    """
    candidates: dict[int, list[dict]] = defaultdict(list)

    for strategy in sorted(strategy_by_problem.keys()):
        for pid, result in strategy_by_problem[strategy].items():
            sa = (result.get("solver_answer") or "").upper()
            cs = (result.get("correct_solution") or "").upper()
            if not cs or sa != cs:
                continue  # only successful solves

            trace = result.get("trace", [])
            # Find the most complete idea whose output matches
            best = None
            best_nn = -1
            for entry in trace:
                if not isinstance(entry, (list, tuple)) or len(entry) < 3:
                    continue
                facets = entry[2] if isinstance(entry[2], dict) else {}
                out = facets.get("output")
                if out is not None and str(out).upper() == cs:
                    nn = sum(facets.get(k) is not None for k in _FACET_KEYS)
                    if nn > best_nn:
                        best = facets
                        best_nn = nn

            if best is not None:
                candidates[pid].append({
                    "strategy": strategy,
                    "correct_solution": cs,
                    "raw_clue": result.get("raw_clue", ""),
                    "facets": best,
                    "n_non_null": best_nn,
                })

    return dict(candidates)


def save_gold_candidates(
    candidates: dict[int, list[dict]],
    output_path: str,
):
    """Save gold candidates for manual inspection.

    Auto-selects gold when:
      - All successful strategies agree on facets, OR
      - Only one strategy solved it (single candidate).

    For non-auto cases, categorizes the reason:
      - "disagree": multiple strategies solved but produced different facets.

    Output format: {problem_id: {candidates: [...], gold: <facets|null>, ...}}
    The user fills in "gold" for entries where it's null.
    """
    output = {}
    # Track categories for console summary
    auto_agree: list[int] = []     # all agree
    auto_single: list[int] = []    # only one candidate
    needs_review: list[int] = []   # multiple candidates disagree

    for pid in sorted(candidates.keys()):
        cands = candidates[pid]
        # Canonical facet signatures to detect agreement
        facet_keys_set = set()
        for c in cands:
            fk = tuple(
                _facet_key(c["facets"], k) for k in _FACET_KEYS
            )
            facet_keys_set.add(fk)

        auto_gold = None
        reason = None

        if len(cands) == 1:
            # Only one strategy solved — auto-select
            auto_gold = cands[0]["facets"]
            reason = "single_solver"
            auto_single.append(pid)
        elif len(facet_keys_set) == 1:
            # All successful strategies agree — auto-select most complete
            best = max(cands, key=lambda c: c["n_non_null"])
            auto_gold = best["facets"]
            reason = "all_agree"
            auto_agree.append(pid)
        else:
            reason = "disagree"
            needs_review.append(pid)

        output[str(pid)] = {
            "clue": cands[0]["raw_clue"] if cands else "",
            "correct_solution": cands[0]["correct_solution"] if cands else "",
            "candidates": [
                {
                    "strategy": c["strategy"],
                    "facets": c["facets"],
                    "n_non_null": c["n_non_null"],
                }
                for c in cands
            ],
            "gold": auto_gold,
            "auto_selected": auto_gold is not None,
            "selection_reason": reason,
        }

    Path(output_path).write_text(json.dumps(output, indent=2))

    n_total = len(output)
    n_auto = len(auto_agree) + len(auto_single)
    print(f"  Saved {n_total} gold candidates to: {output_path}")
    print(f"    Auto-selected: {n_auto}")
    print(f"      All strategies agree:  {len(auto_agree)} "
          f"(pids: {auto_agree})")
    print(f"      Single solver:         {len(auto_single)} "
          f"(pids: {auto_single})")
    if needs_review:
        print(f"    Need manual review:      {len(needs_review)} "
              f"(multiple solvers disagree on facets)")
        for pid in needs_review:
            cands = candidates[pid]
            clue = cands[0]["raw_clue"][:55] if cands else ""
            strats = [c["strategy"][:15] for c in cands]
            print(f"      pid={pid:3d}: {clue:<55s}  solvers: {', '.join(strats)}")

    return output


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 1b: Code gold ideas from opus experiment logs
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_opus_solutions(opus_dir: str) -> list[dict]:
    """Read all opus experiment outputs and extract solution info.

    Returns list of dicts with:
      problem_id, problem (clue text), answer, solution_text
    """
    results = []
    for subdir in sorted(glob.glob(os.path.join(opus_dir, "*"))):
        if not os.path.isdir(subdir):
            continue
        json_files = glob.glob(os.path.join(subdir, "*.json"))
        if not json_files:
            continue
        data = json.loads(Path(json_files[0]).read_text())

        pid = int(data["metadata"]["problem_id"])
        problem = data["metadata"]["problem"]
        answer = data.get("final_answer", "")

        # Find the give_solution response
        solution_text = ""
        for node in data.get("execution", []):
            for branch in node.get("branches", []):
                for t in branch.get("trace", []):
                    if t.get("node_id") == "give_solution":
                        solution_text = t["response"]

        results.append({
            "problem_id": pid,
            "problem": problem,
            "answer": answer,
            "solution_text": solution_text,
        })

    return sorted(results, key=lambda r: r["problem_id"])


def _parse_gold_from_response(response_text: str) -> dict | None:
    """Parse a gold facets dict from an LLM response.

    Expects a code block containing `gold = {...}`.
    """
    # Try to find code block with gold = {...}
    code_blocks = re.findall(r"```python\n(.*?)```", response_text, re.DOTALL)
    if not code_blocks:
        code_blocks = [response_text]

    for block in code_blocks:
        # Try exec approach
        try:
            namespace = {}
            exec(block, namespace)
            gold = namespace.get("gold")
            if isinstance(gold, dict):
                return gold
        except Exception:
            pass

    # Fallback: try to find dict literal after "gold = "
    for block in code_blocks:
        m = re.search(r'gold\s*=\s*(\{.*\})', block, re.DOTALL)
        if m:
            try:
                return ast.literal_eval(m.group(1))
            except (ValueError, SyntaxError):
                pass

    return None


def code_gold_from_opus(
    opus_dir: str,
    output_dir: str,
    client=None,
    model: str = "claude-opus-4-6",
    provider: str = "claude",
) -> dict[str, dict]:
    """Code gold ideas for all problems using opus experiment outputs.

    For each problem:
      1. Extract the opus solution explanation
      2. Send to LLM with PROMPT_GOLD to get faceted coding
      3. Cache results per-problem

    Returns: {str(pid): {"clue": ..., "correct_solution": ..., "gold": {...}}}
    """
    os.makedirs(output_dir, exist_ok=True)
    cache_dir = os.path.join(output_dir, "opus_gold_cache")
    os.makedirs(cache_dir, exist_ok=True)

    solutions = _extract_opus_solutions(opus_dir)
    print(f"  Found {len(solutions)} opus solutions in {opus_dir}")

    gold_results = {}

    for sol in solutions:
        pid = sol["problem_id"]
        cache_path = os.path.join(cache_dir, f"pid_{pid:03d}_gold.json")

        # Check cache
        if os.path.isfile(cache_path):
            cached = json.loads(Path(cache_path).read_text())
            gold_results[str(pid)] = cached
            continue

        # Build prompt
        clue_text, clue_enum = strip_enumeration(sol["problem"])
        prompt = PROMPT_GOLD.format(
            clue_text=clue_text,
            clue_enum=clue_enum,
            answer=sol["answer"],
            solution_text=sol["solution_text"],
        )

        # Call LLM
        if client is None:
            from llm_clients import create_client
            client = create_client(provider, model=model, temperature=0.2)

        print(f"  Coding gold for pid={pid}: {sol['problem'][:50]}...")
        response = client.generate(prompt)

        # Save raw response
        raw_path = os.path.join(cache_dir, f"pid_{pid:03d}_raw.txt")
        Path(raw_path).write_text(response)

        # Parse
        gold_facets = _parse_gold_from_response(response)
        if gold_facets is None:
            print(f"    WARNING: Failed to parse gold for pid={pid}")
            gold_facets = {}

        entry = {
            "clue": sol["problem"],
            "correct_solution": sol["answer"],
            "gold": gold_facets,
            "source": "opus_generate_vars",
        }

        # Cache
        Path(cache_path).write_text(json.dumps(entry, indent=2))
        gold_results[str(pid)] = entry

    return gold_results


def merge_gold_sources(
    opus_gold: dict[str, dict],
    deepseek_candidates: dict[int, list[dict]] | None = None,
) -> dict[str, dict]:
    """Merge opus-coded gold with deepseek candidate info.

    Opus gold is authoritative. Deepseek candidates are added for
    comparison but don't override the gold.

    Returns: complete gold_ideas dict for all problems.
    """
    output = {}

    for pid_str, opus_entry in sorted(opus_gold.items(), key=lambda x: int(x[0])):
        pid = int(pid_str)
        entry = {
            "clue": opus_entry["clue"],
            "correct_solution": opus_entry["correct_solution"],
            "gold": opus_entry["gold"],
            "auto_selected": True,
            "selection_reason": "opus_coded",
            "source": "opus_generate_vars",
        }

        # Add deepseek candidates if available
        if deepseek_candidates and pid in deepseek_candidates:
            entry["deepseek_candidates"] = [
                {
                    "strategy": c["strategy"],
                    "facets": c["facets"],
                    "n_non_null": c["n_non_null"],
                }
                for c in deepseek_candidates[pid]
            ]

        output[pid_str] = entry

    return output


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 2: Facet coverage analysis against gold
# ═══════════════════════════════════════════════════════════════════════════════

def _facet_match(idea_facets: dict, gold_facets: dict, facet: str) -> bool:
    """Check if an idea matches the gold on a specific facet.

    For 'parse': match on the text value (ignoring position).
    For 'mechanism': match if the gold's mechanisms are a subset of the idea's.
    For 'execution': match if the gold's execution steps are all present.
    For 'output': exact match (case-insensitive string).
    """
    gold_val = gold_facets.get(facet)
    idea_val = idea_facets.get(facet)

    if gold_val is None:
        return True  # no gold value to match
    if idea_val is None:
        return False

    if facet == "parse":
        # For "whole" position (no text), match on position only
        if isinstance(gold_val, dict) and gold_val.get("position") == "whole":
            if isinstance(idea_val, dict):
                return idea_val.get("position") == "whole"
            return False
        # Match on text value, stripping trailing punctuation
        g_text = gold_val.get("text", "") if isinstance(gold_val, dict) else str(gold_val)
        i_text = idea_val.get("text", "") if isinstance(idea_val, dict) else str(idea_val)
        if g_text is None or i_text is None:
            return g_text is None and i_text is None
        g_norm = g_text.strip().rstrip("?!.,;:\"'").upper()
        i_norm = i_text.strip().rstrip("?!.,;:\"'").upper()
        return g_norm == i_norm

    if facet == "mechanism":
        # Gold mechanisms should be subset of idea's mechanisms
        g_mechs = set(gold_val) if isinstance(gold_val, list) else {str(gold_val)}
        i_mechs = set(idea_val) if isinstance(idea_val, list) else {str(idea_val)}
        return g_mechs <= i_mechs

    if facet == "output":
        return str(gold_val).upper() == str(idea_val).upper()

    if facet == "execution":
        # Check if the gold's execution results appear in the idea's execution.
        # Exact structural match is too strict since coding style varies.
        # Instead: for each gold step's result, check if any idea step
        # produced the same result (case-insensitive).
        if not isinstance(gold_val, list) or not isinstance(idea_val, list):
            return _facet_key(idea_facets, facet) == _facet_key(gold_facets, facet)

        gold_results = set()
        for step in gold_val:
            if isinstance(step, dict):
                r = step.get("result", "")
                if r and isinstance(r, str):
                    gold_results.add(r.upper())

        idea_results = set()
        for step in idea_val:
            if isinstance(step, dict):
                r = step.get("result", "")
                if r and isinstance(r, str):
                    idea_results.add(r.upper())

        # Gold results should be a subset of idea results
        if not gold_results:
            return True
        return gold_results <= idea_results

    # Fallback: canonical comparison
    return _facet_key(idea_facets, facet) == _facet_key(gold_facets, facet)


def analyze_facet_coverage(
    strategy_by_problem: dict[str, dict[int, dict]],
    gold_ideas: dict[str, dict],
    output_dir: str,
) -> dict:
    """Compare each strategy's ideas against gold, per problem per facet.

    Args:
        strategy_by_problem: {strategy: {pid: coded_result}}
        gold_ideas: loaded gold JSON {str(pid): {gold: facets_dict, ...}}
        output_dir: where to save results

    Returns: analysis results dict
    """
    os.makedirs(output_dir, exist_ok=True)
    strategies = sorted(strategy_by_problem.keys())

    # Per-strategy, per-facet hit counts
    # hit = at least one idea matched the gold on that facet
    facet_hits: dict[str, dict[str, list[bool]]] = {
        s: {f: [] for f in _FACET_KEYS} for s in strategies
    }
    # Per-problem detail
    problem_detail: list[dict] = []

    for pid_str, gold_entry in sorted(gold_ideas.items(), key=lambda x: int(x[0])):
        gold_facets = gold_entry.get("gold")
        if gold_facets is None:
            continue  # not yet curated

        pid = int(pid_str)

        for strategy in strategies:
            result = strategy_by_problem.get(strategy, {}).get(pid)
            if result is None:
                continue

            trace = result.get("trace", [])
            ideas = [
                entry[2] for entry in trace
                if isinstance(entry, (list, tuple)) and len(entry) >= 3
                and isinstance(entry[2], dict)
            ]

            sa = (result.get("solver_answer") or "").upper()
            cs = (result.get("correct_solution") or "").upper()
            solved = sa == cs if cs else None

            detail = {
                "problem_id": pid,
                "strategy": strategy,
                "solved": solved,
                "n_ideas": len(ideas),
                "facets": {},
            }

            for facet in _FACET_KEYS:
                gold_val = gold_facets.get(facet)
                if gold_val is None:
                    detail["facets"][facet] = {"gold_null": True, "matched": True}
                    facet_hits[strategy][facet].append(True)
                    continue

                matched = any(_facet_match(idea, gold_facets, facet)
                              for idea in ideas)
                facet_hits[strategy][facet].append(matched)
                detail["facets"][facet] = {
                    "gold_null": False,
                    "matched": matched,
                }

            problem_detail.append(detail)

    # ── Build per-strategy summaries ─────────────────────────────────────
    summary: dict[str, dict] = {}
    for strategy in strategies:
        strat_details = [d for d in problem_detail if d["strategy"] == strategy]
        strat_summary = {}
        for subset_label, subset in [("all", strat_details),
                                      ("failed", [d for d in strat_details
                                                   if d["solved"] is False])]:
            for facet in _FACET_KEYS:
                relevant = [d for d in subset
                            if not d["facets"][facet]["gold_null"]]
                if relevant:
                    n_hit = sum(1 for d in relevant
                                if d["facets"][facet]["matched"])
                    strat_summary[f"{subset_label}_{facet}"] = {
                        "hit": n_hit, "total": len(relevant),
                        "rate": round(n_hit / len(relevant), 3),
                    }
                else:
                    strat_summary[f"{subset_label}_{facet}"] = {
                        "hit": 0, "total": 0, "rate": None,
                    }
        summary[strategy] = strat_summary

    # ── Format diagnostic report ──────────────────────────────────────
    lines = ["\n══════════════════════════════════════════════════════════════"]
    lines.append("  Gold Facet Bottleneck Analysis")
    lines.append("══════════════════════════════════════════════════════════════")
    lines.append("")
    lines.append("  For each strategy's FAILED problems: what fraction of the")
    lines.append("  time did the solver generate at least one idea that matched")
    lines.append("  the gold on each facet?  Low hit rate = bottleneck facet.")
    lines.append("")

    # ── Failed-only table (the key diagnostic) ────────────────────────
    hdr = f"  {'strategy':<32s}  {'n_fail':>6s}"
    for f in _FACET_KEYS:
        hdr += f"  {f:>11s}"
    lines.append(hdr)
    lines.append("  " + "─" * (len(hdr) - 2))

    for strategy in strategies:
        strat_details = [d for d in problem_detail if d["strategy"] == strategy]
        failed = [d for d in strat_details if d["solved"] is False]
        n_fail = len(failed)
        row = f"  {strategy:<32s}  {n_fail:>6d}"
        for facet in _FACET_KEYS:
            relevant = [d for d in failed
                        if not d["facets"][facet]["gold_null"]]
            if relevant:
                n_hit = sum(1 for d in relevant
                            if d["facets"][facet]["matched"])
                pct = n_hit / len(relevant) * 100
                row += f"  {n_hit:>3d}/{len(relevant):<3d} {pct:>3.0f}%"
            else:
                row += f"  {'n/a':>11s}"
        lines.append(row)

    # ── Bottleneck identification ─────────────────────────────────────
    lines.append("")
    lines.append("  Bottleneck identification (lowest-hit facet on failures):")
    lines.append("")
    for strategy in strategies:
        failed = [d for d in problem_detail
                  if d["strategy"] == strategy and d["solved"] is False]
        if not failed:
            continue
        rates = {}
        for facet in _FACET_KEYS:
            relevant = [d for d in failed
                        if not d["facets"][facet]["gold_null"]]
            if relevant:
                rates[facet] = sum(1 for d in relevant
                                   if d["facets"][facet]["matched"]) / len(relevant)
        if rates:
            # Sort facets by hit rate (excluding output which is trivially 0 on failures)
            non_output = {f: r for f, r in rates.items() if f != "output"}
            if non_output:
                worst = min(non_output, key=non_output.get)
                best = max(non_output, key=non_output.get)
                lines.append(
                    f"  {strategy:<32s}  bottleneck: {worst} "
                    f"({non_output[worst]:.0%})   "
                    f"strength: {best} ({non_output[best]:.0%})"
                )

    # ── Solved-problems table (sanity check) ──────────────────────────
    lines.append("")
    lines.append("  Sanity check — same table for SOLVED problems:")
    lines.append("")
    hdr2 = f"  {'strategy':<32s}  {'n_ok':>6s}"
    for f in _FACET_KEYS:
        hdr2 += f"  {f:>11s}"
    lines.append(hdr2)
    lines.append("  " + "─" * (len(hdr2) - 2))

    for strategy in strategies:
        strat_details = [d for d in problem_detail if d["strategy"] == strategy]
        solved = [d for d in strat_details if d["solved"] is True]
        n_ok = len(solved)
        row = f"  {strategy:<32s}  {n_ok:>6d}"
        for facet in _FACET_KEYS:
            relevant = [d for d in solved
                        if not d["facets"][facet]["gold_null"]]
            if relevant:
                n_hit = sum(1 for d in relevant
                            if d["facets"][facet]["matched"])
                pct = n_hit / len(relevant) * 100
                row += f"  {n_hit:>3d}/{len(relevant):<3d} {pct:>3.0f}%"
            else:
                row += f"  {'n/a':>11s}"
        lines.append(row)

    # ── Per-problem detail for failures ────────────────────────────────
    lines.append("")
    lines.append("  Per-problem detail (failed problems, missed gold facets):")
    lines.append("")
    for d in problem_detail:
        if d["solved"] is not False:
            continue
        missed = [f for f in _FACET_KEYS
                  if not d["facets"][f]["gold_null"] and not d["facets"][f]["matched"]]
        found = [f for f in _FACET_KEYS
                 if not d["facets"][f]["gold_null"] and d["facets"][f]["matched"]]
        if missed:
            found_str = ", ".join(found) if found else "none"
            missed_str = ", ".join(missed)
            lines.append(
                f"    pid={d['problem_id']:3d}  {d['strategy']:<32s}  "
                f"found: {found_str:<35s}  missed: {missed_str}"
            )

    report = "\n".join(lines)
    print(report)

    # Save
    report_path = os.path.join(output_dir, "facet_coverage_report.txt")
    Path(report_path).write_text(report)
    print(f"\n  Saved report: {report_path}")

    results = {
        "summary": summary,
        "problem_detail": problem_detail,
    }
    json_path = os.path.join(output_dir, "facet_coverage.json")
    Path(json_path).write_text(json.dumps(results, indent=2))
    print(f"  Saved JSON: {json_path}")

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline integration
# ═══════════════════════════════════════════════════════════════════════════════

def run_gold_analysis(
    strategy_by_problem: dict[str, dict[int, dict]],
    output_dir: str,
) -> dict | None:
    """Run gold analysis pipeline.

    Uses opus-coded gold ideas (gold_ideas_opus.json) as the authoritative
    source for all 43 problems. Falls back to candidate-based gold if the
    opus file doesn't exist.

    Also extracts deepseek candidates for comparison/reference.
    """
    os.makedirs(output_dir, exist_ok=True)

    opus_gold_path = os.path.join(output_dir, "gold_ideas_opus.json")
    gold_path = os.path.join(output_dir, "gold_ideas.json")

    # Try opus gold first (hand-coded from opus generate_vars outputs)
    if os.path.isfile(opus_gold_path):
        gold_data = json.loads(Path(opus_gold_path).read_text())
        n_curated = sum(1 for v in gold_data.values()
                        if v.get("gold") is not None)
        print(f"  Using opus gold: {n_curated}/{len(gold_data)} problems "
              f"from {opus_gold_path}")

        # Also extract deepseek candidates for reference
        candidates = extract_gold_candidates(strategy_by_problem)
        if candidates:
            # Save deepseek candidates alongside for comparison
            save_gold_candidates(candidates,
                                os.path.join(output_dir, "deepseek_candidates.json"))

        return analyze_facet_coverage(strategy_by_problem, gold_data, output_dir)

    # Fallback: old candidate-based approach
    candidates = extract_gold_candidates(strategy_by_problem)
    if not candidates:
        print("  No successful solves found for gold extraction.")
        return None

    if not os.path.isfile(gold_path):
        save_gold_candidates(candidates, gold_path)
        print(f"  → Review and curate: {gold_path}")
        return None

    gold_data = json.loads(Path(gold_path).read_text())
    n_curated = sum(1 for v in gold_data.values() if v.get("gold") is not None)
    print(f"  Gold ideas: {n_curated}/{len(gold_data)} curated")

    if n_curated == 0:
        print(f"  → No curated golds yet. Review: {gold_path}")
        return None

    return analyze_facet_coverage(strategy_by_problem, gold_data, output_dir)
