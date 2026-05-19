"""Merge Inspect AI eval logs + experiment log JSONs into a single results JSON.

Use case: an eval was interrupted and some samples exist only in experiment
logs (not in the .eval file). This script reads .eval logs for scored samples,
then backfills missing samples from experiment log JSON files, scoring them
against the dataset's ground truth.

Usage:
    # Merge eval log with experiment logs for missing samples:
    python merge_eval_logs.py \
        logs/main_run.eval \
        --experiment-logs experiment_logs/minute-cryptic-par2/20260321_.../ \
        --dataset datasets/minute_cryptic_medium.csv \
        --expected-samples 85 \
        -o eval_results/minute-cryptic-par2/combined.json

    # Merge multiple eval logs (e.g. main run + single-sample retry):
    python merge_eval_logs.py \
        logs/main_run.eval \
        logs/sample_76_retry.eval \
        -o eval_results/minute-cryptic-par2/combined.json

    # Both at once:
    python merge_eval_logs.py \
        logs/main_run.eval \
        logs/sample_76_retry.eval \
        --experiment-logs experiment_logs/.../ \
        --dataset datasets/minute_cryptic_medium.csv \
        --expected-samples 85
"""

import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path


def extract_sample_from_eval(sample) -> dict | None:
    """Extract a sample dict from an Inspect AI EvalSample, or None if errored."""
    sid = sample.id
    if isinstance(sid, str):
        try:
            sid = int(sid)
        except ValueError:
            pass

    # Skip samples that errored (no valid output)
    if sample.error and not sample.scores:
        return None

    answer = ""
    correct = False
    if sample.scores:
        for score_name, score in sample.scores.items():
            if hasattr(score, "answer"):
                answer = score.answer or ""
            if hasattr(score, "value"):
                correct = score.value == "C"

    metadata = sample.metadata or {}

    return {
        "id": sid,
        "input": sample.input if isinstance(sample.input, str) else str(sample.input),
        "target": sample.target or "",
        "answer": answer,
        "correct": correct,
        "error": str(sample.error) if sample.error else None,
        "input_tokens": metadata.get("input_tokens", 0),
        "output_tokens": metadata.get("output_tokens", 0),
        "total_tokens": metadata.get("total_tokens", 0),
        "wall_time_seconds": metadata.get("wall_time_seconds", 0),
        "llm_calls": metadata.get("llm_calls", 0),
        "graph_steps": metadata.get("graph_steps", 0),
        "terminated_early": metadata.get("terminated_early", False),
    }


def load_dataset_targets(dataset_path: str) -> dict[int, dict]:
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


def extract_sample_from_experiment_log(
    json_path: Path, targets: dict[int, dict]
) -> dict | None:
    """Extract a sample dict from an experiment log JSON file."""
    with open(json_path) as f:
        data = json.load(f)

    metadata = data.get("metadata", {})
    pid = metadata.get("problem_id")
    if pid is None:
        return None
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return None

    answer = data.get("final_answer", "").strip()
    problem = metadata.get("problem", "")

    # Score against ground truth
    target_info = targets.get(pid)
    if target_info is None:
        print(f"  ⚠ No ground truth for ID {pid}, marking incorrect")
        correct = False
        target = ""
    else:
        target = target_info["solution"]
        correct = answer.lower() == target.lower()

    # Estimate token counts from execution trace
    execution = data.get("execution", [])
    num_steps = len(execution)

    return {
        "id": pid,
        "input": problem,
        "target": target,
        "answer": answer,
        "correct": correct,
        "error": None,
        "input_tokens": 0,  # not available from experiment logs
        "output_tokens": 0,
        "total_tokens": 0,
        "wall_time_seconds": 0,
        "llm_calls": num_steps,
        "graph_steps": num_steps,
        "terminated_early": data.get("terminated_early", False),
    }


def find_best_experiment_log(exp_dir: Path, sample_id: int) -> Path | None:
    """Find the latest experiment log JSON for a given sample ID.

    Looks for subdirectories matching *_{sample_id}/ that contain a JSON file.
    Returns the JSON path from the most recent (by dirname) match.
    """
    candidates = []
    for d in exp_dir.iterdir():
        if not d.is_dir():
            continue
        parts = d.name.split("_")
        if not parts:
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
    # Latest by directory name (timestamp-sorted)
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def main():
    parser = argparse.ArgumentParser(
        description="Merge Inspect AI eval logs + experiment logs into one results JSON"
    )
    parser.add_argument(
        "logs", nargs="+",
        help="Paths to .eval log files (later logs override earlier for duplicate IDs)"
    )
    parser.add_argument(
        "--experiment-logs", "-e", default=None,
        help="Path to experiment logs directory (for backfilling samples missing "
             "from .eval logs). Searches subdirectories for JSON result files."
    )
    parser.add_argument(
        "--dataset", "-d", default=None,
        help="Path to dataset CSV (required when using --experiment-logs, for scoring)"
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Output JSON path (auto-generated if omitted)"
    )
    parser.add_argument(
        "--expected-samples", "-n", type=int, default=None,
        help="Expected total number of samples (warns if mismatch)"
    )
    args = parser.parse_args()

    if args.experiment_logs and not args.dataset:
        parser.error("--dataset is required when using --experiment-logs (for scoring)")

    # Lazy import so the script fails fast on bad args
    from inspect_ai.log import read_eval_log

    # Collect samples from all eval logs, later logs override earlier ones
    samples_by_id: dict[int | str, dict] = {}
    first_log = None

    for log_path in args.logs:
        print(f"Reading eval log: {log_path}...")
        log = read_eval_log(log_path)
        if first_log is None:
            first_log = log

        if not log.samples:
            print(f"  (no samples)")
            continue

        added = 0
        skipped = 0
        for sample in log.samples:
            extracted = extract_sample_from_eval(sample)
            if extracted is not None:
                samples_by_id[extracted["id"]] = extracted
                added += 1
            else:
                skipped += 1
        print(f"  {added} valid samples, {skipped} errored/skipped")

    # Backfill missing samples from experiment logs
    if args.experiment_logs and args.expected_samples:
        exp_dir = Path(args.experiment_logs)
        targets = load_dataset_targets(args.dataset)
        all_expected = set(range(1, args.expected_samples + 1))
        missing = sorted(all_expected - set(samples_by_id.keys()))

        if missing:
            print(f"\nBackfilling {len(missing)} missing samples from experiment logs...")
            backfilled = 0
            for sid in missing:
                json_path = find_best_experiment_log(exp_dir, sid)
                if json_path is None:
                    print(f"  ✗ ID {sid}: no experiment log JSON found")
                    continue
                sample = extract_sample_from_experiment_log(json_path, targets)
                if sample is not None:
                    samples_by_id[sample["id"]] = sample
                    status = "✓" if sample["correct"] else "✗"
                    print(f"  {status} ID {sid}: answer={sample['answer']!r} "
                          f"(target={sample['target']!r}) from {json_path.name}")
                    backfilled += 1
                else:
                    print(f"  ✗ ID {sid}: could not parse {json_path}")
            print(f"  Backfilled {backfilled}/{len(missing)} missing samples")

    # Sort by ID
    samples = sorted(samples_by_id.values(), key=lambda s: s["id"])
    num_correct = sum(1 for s in samples if s["correct"])
    num_samples = len(samples)
    accuracy = num_correct / num_samples if num_samples > 0 else 0.0

    if args.expected_samples and num_samples != args.expected_samples:
        print(f"\n⚠ WARNING: Expected {args.expected_samples} samples but got {num_samples}")
        all_ids = set(range(1, args.expected_samples + 1))
        got_ids = set(s["id"] for s in samples)
        missing = sorted(all_ids - got_ids)
        if missing:
            print(f"  Still missing IDs: {missing}")

    # Compute stderr (binomial)
    stderr = math.sqrt(accuracy * (1 - accuracy) / num_samples) if num_samples > 0 else 0.0

    # Build result dict matching run_eval.py format
    task_args = first_log.eval.task_args if first_log else {}
    result = {
        "timestamp": datetime.now().isoformat(),
        "config_path": task_args.get("config_path", ""),
        "config_name": Path(task_args.get("config_path", "")).stem,
        "dataset": task_args.get("dataset_path", ""),
        "provider": task_args.get("provider_override", ""),
        "model": first_log.eval.model if first_log else "",
        "thinking": task_args.get("thinking", False),
        "base_url": task_args.get("base_url", ""),
        "epochs": 1,
        "epochs_reducer": None,
        "eval_status": "success" if num_samples > 0 else "error",
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
        "merged_from": [str(p) for p in args.logs],
    }
    if args.experiment_logs:
        result["backfilled_from"] = str(args.experiment_logs)

    # Output path
    if args.output:
        out_path = Path(args.output)
    else:
        config_name = result["config_name"]
        model_name = str(result["model"]).replace(":", "_").replace("/", "_")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = Path("eval_results") / f"{ts}_{config_name}_{model_name}_merged.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n✓ Merged {num_samples} samples — accuracy: {accuracy:.4f} ({num_correct}/{num_samples})")
    print(f"  Written to: {out_path}")


if __name__ == "__main__":
    main()
