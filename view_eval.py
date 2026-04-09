#!/usr/bin/env python3
"""Viewer for .eval log files (Inspect AI format).

These are zip archives containing JSON files:
  _journal/start.json        - eval metadata
  _journal/summaries/*.json  - per-batch result summaries
  samples/*_epoch_*.json     - individual sample results
"""

import argparse
import json
import sys
import zipfile
from pathlib import Path


def load_eval(path):
    """Load an .eval zip and return (start_info, summaries, samples)."""
    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()

        start = json.loads(zf.read("_journal/start.json"))

        summaries = []
        for name in sorted(n for n in names if n.startswith("_journal/summaries/")):
            summaries.extend(json.loads(zf.read(name)))

        samples = []
        for name in sorted(
            (n for n in names if n.startswith("samples/")),
            key=lambda n: int(n.split("/")[1].split("_")[0]),
        ):
            samples.append(json.loads(zf.read(name)))

    return start, summaries, samples


def fmt_score(scores):
    """Format scores dict into a compact string."""
    parts = []
    for scorer, result in scores.items():
        val = result.get("value", "?")
        mark = "\033[32m✓\033[0m" if val == "C" else "\033[31m✗\033[0m"
        parts.append(f"{mark} {val}")
        if result.get("answer"):
            parts.append(f'  answer="{result["answer"]}"')
    return " ".join(parts)


def print_header(start):
    """Print eval metadata."""
    ev = start.get("eval", {})
    task_args = ev.get("task_args", {})
    dataset = ev.get("dataset", {})

    print("\033[1m" + "=" * 70 + "\033[0m")
    print(f"\033[1mEval:\033[0m {ev.get('task_display_name', '?')}  "
          f"(run: {ev.get('run_id', '?')[:12]}...)")
    print(f"\033[1mCreated:\033[0m {ev.get('created', '?')}")
    print(f"\033[1mDataset:\033[0m {dataset.get('name', '?')} "
          f"({dataset.get('samples', '?')} samples)")
    print(f"\033[1mConfig:\033[0m {task_args.get('config_path', '?')}")
    print(f"\033[1mModel:\033[0m {task_args.get('model_override', '?')} "
          f"(provider: {task_args.get('provider_override', '?')})")
    if task_args.get("thinking"):
        print(f"\033[1mThinking:\033[0m enabled")
    print("\033[1m" + "=" * 70 + "\033[0m")


def print_summary_table(summaries):
    """Print a compact results table from summaries."""
    if not summaries:
        print("No summaries found.")
        return

    correct = sum(1 for s in summaries
                  if any(v.get("value") == "C"
                         for v in s.get("scores", {}).values()))
    total = len(summaries)
    pct = correct / total * 100 if total else 0

    print(f"\n\033[1mResults: {correct}/{total} correct ({pct:.1f}%)\033[0m\n")

    # Token stats
    tok_in = sum(s.get("metadata", {}).get("input_tokens", 0) for s in summaries)
    tok_out = sum(s.get("metadata", {}).get("output_tokens", 0) for s in summaries)
    wall = sum(s.get("total_time", 0) for s in summaries)
    print(f"  Tokens: {tok_in:,} in / {tok_out:,} out / {tok_in + tok_out:,} total")
    print(f"  Wall time: {wall:.1f}s ({wall/60:.1f}m)")
    print()


def print_samples_table(summaries):
    """Print per-sample results as a compact table."""
    if not summaries:
        return

    print(f"{'#':>4}  {'Score':6}  {'Time':>6}  {'Tokens':>8}  {'Input (truncated)':40}  Target")
    print("-" * 120)
    for s in summaries:
        sid = s.get("id", "?")
        scores = s.get("scores", {})
        first_score = next(iter(scores.values()), {})
        val = first_score.get("value", "?")
        color = "\033[32m" if val == "C" else "\033[31m"

        time_s = s.get("total_time", 0)
        tokens = s.get("metadata", {}).get("total_tokens", 0)
        inp = s.get("input", "")[:40].replace("\n", " ")
        target = s.get("target", "")[:40]

        print(f"{sid:>4}  {color}{val:6}\033[0m  {time_s:>5.1f}s  {tokens:>8,}  {inp:40}  {target}")


def print_sample_detail(sample, idx=None):
    """Print full detail for one sample."""
    label = f"Sample {sample.get('id', idx)}"
    print(f"\n\033[1m--- {label} ---\033[0m")
    print(f"\033[1mInput:\033[0m\n  {sample.get('input', '?')[:200]}")
    print(f"\033[1mTarget:\033[0m {sample.get('target', '?')}")

    output = sample.get("output", {})
    completion = output.get("completion", "")
    print(f"\033[1mCompletion:\033[0m {completion}")
    print(f"\033[1mScores:\033[0m {fmt_score(sample.get('scores', {}))}")

    meta = sample.get("metadata", {})
    if meta:
        print(f"\033[1mTokens:\033[0m {meta.get('total_tokens', '?'):,}  "
              f"({meta.get('input_tokens', 0):,} in / {meta.get('output_tokens', 0):,} out)  "
              f"Wall: {meta.get('wall_time_seconds', 0):.1f}s  "
              f"LLM calls: {meta.get('llm_calls', '?')}")

    # Show messages if available
    messages = sample.get("messages", [])
    if len(messages) > 1:
        print(f"\033[1mMessages ({len(messages)}):\033[0m")
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if len(content) > 200:
                content = content[:200] + "..."
            print(f"  [{role}] {content}")


def main():
    parser = argparse.ArgumentParser(
        description="View .eval log files in readable format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  %(prog)s logs/my_eval.eval                    # overview + results table
  %(prog)s logs/my_eval.eval --samples          # show per-sample table
  %(prog)s logs/my_eval.eval --detail 1 5 10    # show full detail for samples 1, 5, 10
  %(prog)s logs/my_eval.eval --detail all       # full detail for all samples
  %(prog)s logs/my_eval.eval --errors           # show only incorrect samples
  %(prog)s logs/my_eval.eval --json             # dump raw start.json
  %(prog)s logs/*.eval --compare                # compare multiple eval files
""",
    )
    parser.add_argument("files", nargs="+", help=".eval file(s) to view")
    parser.add_argument("--samples", "-s", action="store_true",
                        help="Show per-sample results table")
    parser.add_argument("--detail", "-d", nargs="*", default=None,
                        help="Show full detail for sample IDs (or 'all')")
    parser.add_argument("--errors", "-e", action="store_true",
                        help="Show only incorrect samples")
    parser.add_argument("--json", "-j", action="store_true",
                        help="Dump raw eval metadata as JSON")
    parser.add_argument("--compare", "-c", action="store_true",
                        help="Compare accuracy across multiple eval files")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable colored output")
    args = parser.parse_args()

    _print = print
    if args.no_color:
        import re
        _orig_print = _print
        def _print(*a, **kw):
            text = " ".join(str(x) for x in a)
            text = re.sub(r"\033\[[0-9;]*m", "", text)
            _orig_print(text, **kw)

    # Monkey-patch builtins so all helper functions use our print
    import builtins
    builtins.print = _print

    if args.compare and len(args.files) > 1:
        print(f"\n\033[1m{'File':60} {'Correct':>8} {'Total':>6} {'Accuracy':>9} {'Model':20} Config\033[0m")
        print("-" * 130)
        for fpath in sorted(args.files):
            try:
                start, summaries, _ = load_eval(fpath)
                ev = start.get("eval", {})
                ta = ev.get("task_args", {})
                correct = sum(1 for s in summaries
                              if any(v.get("value") == "C"
                                     for v in s.get("scores", {}).values()))
                total = len(summaries)
                pct = correct / total * 100 if total else 0
                model = ta.get("model_override", "?")
                config = Path(ta.get("config_path", "?")).stem
                fname = Path(fpath).name[:58]
                print(f"{fname:60} {correct:>8} {total:>6} {pct:>8.1f}%  {model:20} {config}")
            except Exception as e:
                print(f"{Path(fpath).name:60} ERROR: {e}")
        return

    for fpath in args.files:
        try:
            start, summaries, samples = load_eval(fpath)
        except Exception as e:
            print(f"Error reading {fpath}: {e}", file=sys.stderr)
            continue

        if args.json:
            print(json.dumps(start, indent=2))
            continue

        print_header(start)
        print_summary_table(summaries)

        if args.samples or args.errors:
            if args.errors:
                summaries = [s for s in summaries
                             if not any(v.get("value") == "C"
                                        for v in s.get("scores", {}).values())]
                print(f"\033[1mShowing {len(summaries)} incorrect samples:\033[0m")
            print_samples_table(summaries)

        if args.detail is not None:
            detail_ids = args.detail if args.detail else ["all"]
            if "all" in detail_ids:
                for s in samples:
                    if args.errors:
                        scores = s.get("scores", {})
                        if any(v.get("value") == "C" for v in scores.values()):
                            continue
                    print_sample_detail(s)
            else:
                id_set = set(int(x) for x in detail_ids)
                for s in samples:
                    if s.get("id") in id_set:
                        print_sample_detail(s)

        if len(args.files) > 1:
            print("\n")


if __name__ == "__main__":
    main()
