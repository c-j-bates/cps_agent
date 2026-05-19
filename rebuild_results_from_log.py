#!/usr/bin/env python3
"""Rebuild an eval_results JSON from a completed Inspect AI eval log.

Usage:
    python rebuild_results_from_log.py <eval_log_path> \
        --config <config_yaml> --dataset <csv_path> \
        --provider claude --model claude-opus-4-6 \
        [--thinking max] [--exp-name minute-cryptic-par2] \
        [--experiment-log-dir <dir>] [-o output.json]

This is useful when `inspect eval-retry` completed successfully but
run_eval.py's results JSON was written by an earlier interrupted run.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from inspect_ai.log import read_eval_log


def _extract_results(log, args) -> dict:
    """Mirror run_eval._extract_results_json but from CLI args."""
    config_name = Path(args.config).stem

    result = {
        "timestamp": datetime.now().isoformat(),
        "config_path": args.config,
        "config_name": config_name,
        "dataset": args.dataset,
        "provider": args.provider or "config_default",
        "model": args.model or "config_default",
        "thinking": args.thinking if args.thinking else False,
        "base_url": "",
        "epochs": 1,
        "epochs_reducer": None,
        "eval_status": log.status,
        "eval_error": str(log.error) if getattr(log, "error", None) else None,
        "metrics": {},
        "samples": [],
    }

    if args.experiment_log_dir:
        result["experiment_log_dir"] = args.experiment_log_dir

    # Extract aggregate metrics
    if log.results:
        for score_group in log.results.scores:
            for k, v in score_group.metrics.items():
                result["metrics"][k] = v.value

    # Extract per-sample results
    if log.samples:
        for sample in log.samples:
            sample_data = {
                "id": sample.id,
                "input": sample.input[:200] if isinstance(sample.input, str) else str(sample.input)[:200],
                "target": sample.target,
                "answer": None,
                "correct": False,
                "error": sample.error.message if getattr(sample, "error", None) else None,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "wall_time_seconds": 0,
                "llm_calls": 0,
                "graph_steps": 0,
                "terminated_early": False,
            }

            if sample.scores:
                for scorer_name, score in sample.scores.items():
                    sample_data["answer"] = score.answer
                    sample_data["correct"] = score.value == "C"

            metadata = sample.metadata or {}
            for key in ["input_tokens", "output_tokens", "total_tokens",
                        "wall_time_seconds", "llm_calls", "graph_steps",
                        "terminated_early"]:
                if key in metadata:
                    sample_data[key] = metadata[key]

            if "solver_error" in metadata:
                sample_data["solver_error"] = metadata["solver_error"]

            result["samples"].append(sample_data)

    # Compute aggregates
    samples = result["samples"]
    if samples:
        n = len(samples)
        n_correct = sum(1 for s in samples if s["correct"])
        result["aggregate"] = {
            "num_samples": n,
            "num_correct": n_correct,
            "accuracy": n_correct / n,
            "total_input_tokens": sum(s["input_tokens"] for s in samples),
            "total_output_tokens": sum(s["output_tokens"] for s in samples),
            "total_tokens": sum(s["total_tokens"] for s in samples),
            "mean_tokens_per_sample": sum(s["total_tokens"] for s in samples) / n,
            "total_wall_time": sum(s["wall_time_seconds"] for s in samples),
            "total_llm_calls": sum(s["llm_calls"] for s in samples),
            "mean_graph_steps": sum(s["graph_steps"] for s in samples) / n,
        }

    return result


def main():
    parser = argparse.ArgumentParser(description="Rebuild results JSON from Inspect eval log")
    parser.add_argument("eval_log", help="Path to the .eval log file")
    parser.add_argument("--config", required=True, help="Config YAML path (for naming)")
    parser.add_argument("--dataset", required=True, help="Dataset CSV path")
    parser.add_argument("--provider", default="claude")
    parser.add_argument("--model", default="config_default")
    parser.add_argument("--thinking", default=False)
    parser.add_argument("--exp-name", default="", help="Subdirectory under eval_results/")
    parser.add_argument("--experiment-log-dir", default="", help="Path to experiment logs dir")
    parser.add_argument("-o", "--output", help="Output path (auto-generated if omitted)")
    args = parser.parse_args()

    log = read_eval_log(args.eval_log)
    results = _extract_results(log, args)

    if args.output:
        out_path = Path(args.output)
    else:
        results_dir = Path("eval_results")
        if args.exp_name:
            results_dir = results_dir / args.exp_name
        results_dir.mkdir(parents=True, exist_ok=True)

        config_name = Path(args.config).stem
        model_name = (args.model or "default").replace(":", "_").replace("/", "_")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = results_dir / f"{ts}_{config_name}_{model_name}.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    n = results.get("aggregate", {}).get("num_samples", 0)
    acc = results.get("aggregate", {}).get("accuracy", 0)
    print(f"Wrote {out_path}  ({n} samples, accuracy={acc:.3f})")


if __name__ == "__main__":
    main()
