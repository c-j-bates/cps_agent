"""Build a results JSON from experiment log directories, bypassing Inspect AI.

Reads experiment log JSONs + companion .md files, scores answers against the
dataset CSV, and produces output matching the run_eval.py format.

Usage:
    python build_results_from_logs.py \
        --experiment-logs experiment_logs/minute-cryptic-par2/20260321_.../ \
        --dataset datasets/minute_cryptic_medium.csv \
        --config configs/config_minute_cryptic_baseline.yaml \
        --provider claude --model claude-opus-4-6 --thinking max \
        -o eval_results/minute-cryptic-par2/combined.json
"""

import argparse
import csv
import json
import math
import re
from datetime import datetime
from pathlib import Path


# Regex to strip <thinking>...</thinking> blocks from answers
_THINKING_RE = re.compile(r"<thinking>.*?</thinking>\s*", re.DOTALL)


def strip_thinking(text: str) -> str:
    """Remove <thinking>...</thinking> blocks and return the clean answer."""
    return _THINKING_RE.sub("", text).strip()


def load_dataset(dataset_path: str) -> dict[int, dict]:
    """Load ground truth from CSV. Returns {id: {problem, solution}}."""
    targets = {}
    with open(dataset_path, newline="") as f:
        reader = csv.DictReader(f)
        has_id = "id" in (reader.fieldnames or [])
        for i, row in enumerate(reader, start=1):
            pid = int(row["id"].strip()) if has_id else i
            targets[pid] = {
                "problem": row["problem"].strip(),
                "solution": row["solution"].strip(),
            }
    return targets


def find_best_log(exp_dir: Path, sample_id: int) -> Path | None:
    """Find the latest experiment log JSON for a sample ID.

    Directories are named like 20260322_182122_82. We pick the latest
    (by dirname sort) that contains a .json file.
    """
    candidates = []
    for d in exp_dir.iterdir():
        if not d.is_dir():
            continue
        parts = d.name.split("_")
        if len(parts) < 3:
            continue
        try:
            dir_id = int(parts[-1])
        except ValueError:
            continue
        if dir_id != sample_id:
            continue
        jsons = list(d.glob("*.json"))
        if jsons:
            candidates.append((d.name, jsons[0]))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def parse_token_summary_from_md(md_path: Path) -> dict:
    """Parse token counts and wall time from the companion .md file.

    Looks for the Token Summary table:
        | Total input tokens | 1,584 |
        | Total output tokens | 1,257 |
        | Total tokens | 2,841 |
        | Wall clock time | 29.1s |
    """
    result = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "wall_time_seconds": 0.0,
    }
    try:
        text = md_path.read_text()
    except FileNotFoundError:
        return result

    for line in text.splitlines():
        line = line.strip()
        if "Total input tokens" in line:
            m = re.search(r"([\d,]+)", line.split("|")[2])
            if m:
                result["input_tokens"] = int(m.group(1).replace(",", ""))
        elif "Total output tokens" in line:
            m = re.search(r"([\d,]+)", line.split("|")[2])
            if m:
                result["output_tokens"] = int(m.group(1).replace(",", ""))
        elif "Total tokens" in line and "input" not in line and "output" not in line:
            m = re.search(r"([\d,]+)", line.split("|")[2])
            if m:
                result["total_tokens"] = int(m.group(1).replace(",", ""))
        elif "Wall clock time" in line:
            m = re.search(r"([\d.]+)s", line)
            if m:
                result["wall_time_seconds"] = float(m.group(1))

    return result


def parse_experiment_log(json_path: Path, target: dict) -> dict:
    """Parse an experiment log JSON + companion .md into a results sample dict."""
    with open(json_path) as f:
        data = json.load(f)

    raw_answer = data.get("final_answer", "").strip()
    answer = strip_thinking(raw_answer)
    expected = target["solution"]
    correct = answer.lower() == expected.lower()

    execution = data.get("execution", [])
    num_steps = len(execution)

    # Get token counts from companion .md file
    md_path = json_path.with_suffix(".md")
    token_info = parse_token_summary_from_md(md_path)

    return {
        "answer": answer,
        "correct": correct,
        "error": None,
        "input_tokens": token_info["input_tokens"],
        "output_tokens": token_info["output_tokens"],
        "total_tokens": token_info["total_tokens"],
        "wall_time_seconds": round(token_info["wall_time_seconds"], 2),
        "llm_calls": num_steps,
        "graph_steps": num_steps,
        "terminated_early": data.get("terminated_early", False),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Build results JSON from experiment logs (no Inspect AI needed)"
    )
    parser.add_argument(
        "--experiment-logs", "-e", required=True,
        help="Path to experiment logs directory containing per-sample subdirs"
    )
    parser.add_argument(
        "--dataset", "-d", required=True,
        help="Path to dataset CSV (for ground truth scoring)"
    )
    parser.add_argument("--config", "-c", default="", help="Config file path (for metadata)")
    parser.add_argument("--provider", default="", help="Provider name (for metadata)")
    parser.add_argument("--model", default="", help="Model name (for metadata)")
    parser.add_argument("--thinking", default=False, help="Thinking mode (for metadata)")
    parser.add_argument("--output", "-o", default=None, help="Output JSON path")
    parser.add_argument("--exp-name", default=None,
                        help="Experiment name — creates subdirectory under eval_results/ "
                             "(e.g. --exp-name minute-cryptic-par2)")
    args = parser.parse_args()

    dataset = load_dataset(args.dataset)
    exp_dir = Path(args.experiment_logs)
    n_total = len(dataset)

    samples = []
    missing = []

    for sid in sorted(dataset.keys()):
        target = dataset[sid]
        json_path = find_best_log(exp_dir, sid)

        if json_path is None:
            missing.append(sid)
            print(f"  ✗ ID {sid}: no experiment log found")
            continue

        sample_data = parse_experiment_log(json_path, target)
        sample_data["id"] = sid
        sample_data["input"] = target["problem"]
        sample_data["target"] = target["solution"]
        samples.append(sample_data)

        status = "✓" if sample_data["correct"] else "✗"
        print(f"  {status} ID {sid}: {sample_data['answer']!r} (target={target['solution']!r})")

    num_correct = sum(1 for s in samples if s["correct"])
    num_samples = len(samples)
    accuracy = num_correct / num_samples if num_samples > 0 else 0.0
    stderr = math.sqrt(accuracy * (1 - accuracy) / num_samples) if num_samples > 0 else 0.0

    if missing:
        print(f"\n⚠ Missing {len(missing)} samples: {missing}")

    result = {
        "timestamp": datetime.now().isoformat(),
        "config_path": args.config,
        "config_name": Path(args.config).stem if args.config else "",
        "dataset": args.dataset,
        "provider": args.provider,
        "model": args.model,
        "thinking": args.thinking,
        "base_url": "",
        "epochs": 1,
        "epochs_reducer": None,
        "eval_status": "success" if num_samples == n_total else "partial",
        "eval_error": None,
        "metrics": {
            "accuracy": accuracy,
            "stderr": stderr,
        },
        "samples": samples,
        "aggregate": {
            "num_samples": num_samples,
            "num_correct": num_correct,
            "accuracy": accuracy,
            "total_input_tokens": sum(s["input_tokens"] for s in samples),
            "total_output_tokens": sum(s["output_tokens"] for s in samples),
            "total_tokens": sum(s["total_tokens"] for s in samples),
            "avg_wall_time": sum(s["wall_time_seconds"] for s in samples) / num_samples if num_samples else 0,
            "avg_llm_calls": sum(s["llm_calls"] for s in samples) / num_samples if num_samples else 0,
        },
        "built_from_experiment_logs": str(exp_dir),
    }

    if args.output:
        out_path = Path(args.output)
    else:
        config_name = result["config_name"] or "unknown"
        model_name = args.model.replace(":", "_").replace("/", "_") or "unknown"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_dir = Path("eval_results")
        if args.exp_name:
            results_dir = results_dir / args.exp_name
        out_path = results_dir / f"{ts}_{config_name}_{model_name}.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n✓ {num_samples}/{n_total} samples — accuracy: {accuracy:.4f} ({num_correct}/{num_samples})")
    print(f"  Written to: {out_path}")


if __name__ == "__main__":
    main()
